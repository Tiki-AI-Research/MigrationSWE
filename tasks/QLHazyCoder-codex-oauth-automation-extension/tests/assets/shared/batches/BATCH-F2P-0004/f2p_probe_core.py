"""Shared F2P probe core for BATCH-F2P-0004.

The migration under test replaces Python 3.10 ``http.server`` with Flask 3.1.2
inside ``scripts/hotmail_helper.py`` and ``scripts/custom_mail_helper.py``.

Every observation in this batch is an F2P target postcondition: the *same*
semantic predicate must hold only when the request handling path is owned by the
target technology (Flask), and must fail on the source technology
(``http.server``).  The batch extends the preserved-envelope contract that
BATCH-F2P-0003 already exercises, with six independent groups of nodes:

* the preflight envelope (HTTP 204 + CORS + a JSON content type);
* the unknown-path envelope, whose ``error`` echoes the original request target
  exactly as received (query string, percent-encoding and trailing slash
  included);
* the malformed-JSON failure envelope, whose ``error`` reproduces the source
  parse error verbatim;
* the routing envelope, where a documented route is still served when the
  request target carries a query string, when the route path is percent-encoded
  or when the request uses the absolute form (``POST http://host/route``).  The
  target routes on the *decoded* URL path, while the source compares the raw
  request target (query string included) and therefore misses each of these;
* the request-body transfer encoding, where a ``Transfer-Encoding: chunked``
  body must be parsed exactly like the equivalent ``Content-Length`` body.  The
  source only ever reads ``Content-Length`` bytes, so a chunked body arrives
  empty there, while the target hands the de-chunked bytes to the JSON parser;
* route-first dispatch, where routing is resolved before any body is parsed and
  each view parses only the body it consumes.  The source parses the body before
  it dispatches, so a malformed body turns *every* request into the malformed
  envelope; the target answers an unknown path with the 404 envelope and serves
  the body-independent ``custom_mail_helper`` ``POST /health`` and ``POST /env``
  routes with HTTP 200 even when the body is malformed or not decodable;
* the request envelope, where the request framing and a fixed body-size ceiling
  are validated before any route body handling.  The source trusts the declared
  ``Content-Length`` verbatim and imposes no ceiling, so it neither rejects a
  conflicting ``Transfer-Encoding`` nor an unparseable length and happily parses
  an oversized body; the target rejects each of these with the documented error
  envelope.
* the decoded-body ceiling and the ``Transfer-Encoding`` acceptance set, where
  the ``MAX_BODY_BYTES`` ceiling is measured on the *decoded* entity body (so a
  chunked body that decodes to more than 1 MiB is rejected with the 413 envelope
  before any side effect) and the only accepted ``Transfer-Encoding`` is a single
  case-insensitive ``chunked`` coding, answered before routing.  The source never
  inspects ``Transfer-Encoding`` and reads only ``Content-Length``, so it accepts
  or mis-handles all of these; the target answers the documented envelope.
* the request-target byte fidelity and the zero-length entity, where the
  unknown-path envelope echoes the raw request-target octets with the source's
  ISO-8859-1 decoding (so a raw, non-percent-encoded ``0xC3 0xA9`` survives as
  ``Ã©``) instead of a framework-re-encoded form, a matched body-consuming route
  that receives a zero-length entity materialises the JSON payload as ``{}`` and
  runs its own field validation rather than failing JSON parsing, and an empty or
  whitespace-only ``Content-Length`` value is answered with the framing envelope.
  The source decodes the request line the same way but never owns the routing, so
  it cannot satisfy the target-technology ownership signal.

The probe therefore:

* copies the repository ``scripts`` into a scratch work tree so the read-only
  authoring mounts are never mutated;
* boots each helper's real entry point (``python <helper>.py``) as a subprocess
  and drives it over a real TCP socket;
* evaluates the preserved envelope semantics on the raw HTTP responses.

The observable target-technology signal is the HTTP ``Server`` response header:
Flask/Werkzeug emits ``Werkzeug/...`` while ``http.server`` emits
``BaseHTTP/...``.  The batch is intentionally self-contained so it can be added
or removed as a whole to raise or lower the graded difficulty: removing the
whole batch (or dropping a single observation from the projection) can only let
a candidate score higher.
"""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HOTMAIL_PORT = 17373
CUSTOM_PORT = 18374
TARGET_SERVER_MARKER = "Werkzeug"
JSON_CONTENT_TYPE = "application/json; charset=utf-8"
CORS_ORIGIN = "*"
CORS_HEADERS = "Content-Type"
CORS_METHODS = "POST, OPTIONS"
BAD_JSON_BODY = b"{oops"
BAD_JSON_PATH_HOTMAIL = "/messages"
BAD_JSON_PATH_CUSTOM = "/code"

# --- L4 routing probes ----------------------------------------------------
# A documented route must still be served when the request target carries a
# query string, when the route path is percent-encoded, or when the target uses
# the absolute form.  The target parses the request target into a decoded URL
# path before routing; the source compares ``self.path`` verbatim (query string
# and all), so every probe below lands on a different code path there: either
# the unknown-path envelope or its pre-dispatch failure envelope.
QUERY_HEALTH = "/health?x=1"
QUERY_RANDOM = "/random-email?x=1"
QUERY_ENV = "/env?v=2"
QUERY_MESSAGES = "/messages?x=1"
QUERY_APPEND = "/append-account-log?x=1"
QUERY_SYNC = "/sync-account-run-records?x=1"
ENCODED_HEALTH = "/he%61lth"
ENCODED_RANDOM = "/random-%65mail"
ENCODED_SYNC = "/sync-account-run-%72ecords"
RANDOM_EMAIL_PAYLOAD = {"domain": "example.com", "n": 1}
APPEND_QUERY_PAYLOAD = {
    "email": "probe-query@example.com",
    "password": "secret",
    "status": "success",
    "recordedAt": "2026-01-01T00:00:00Z",
    "reason": "probe-query",
}
APPEND_QUERY_EXPECTED = "2026-01-01T00:00:00Z\tprobe-query@example.com\tsecret\tsuccess\tprobe-query"
APPEND_ABSOLUTE_PAYLOAD = {
    "email": "probe-absolute@example.com",
    "password": "secret",
    "status": "success",
    "recordedAt": "2026-01-03T00:00:00Z",
    "reason": "probe-absolute",
}
APPEND_ABSOLUTE_EXPECTED = "2026-01-03T00:00:00Z\tprobe-absolute@example.com\tsecret\tsuccess\tprobe-absolute"
SYNC_QUERY_PAYLOAD = {
    "records": [
        {
            "email": "probe-query@example.com",
            "password": "secret",
            "finalStatus": "success",
            "retryCount": 1,
        }
    ]
}
SYNC_ENCODED_PAYLOAD = {
    "records": [
        {
            "email": "probe-encoded@example.com",
            "password": "secret",
            "finalStatus": "success",
            "retryCount": 1,
        },
        {
            "email": "probe-encoded@example.com",
            "password": "secret",
            "finalStatus": "failed",
            "retryCount": 2,
        },
    ]
}
# --- L5 transfer-encoding probes -----------------------------------------
# A request body framed with ``Transfer-Encoding: chunked`` must be parsed
# exactly like the equivalent ``Content-Length`` body.  The source technology
# only ever reads ``Content-Length`` bytes, so a chunked body arrives empty
# there; the target hands the de-chunked bytes to the JSON parser, so both
# framings are equivalent.  Chunk sizes 5 and 1 exercise multi- and single-byte
# frames respectively.
CHUNK_RANDOM_PAYLOAD = {"domain": "chunked.example.com", "n": 3}
CHUNK_RANDOM_ONE_PAYLOAD = {"domain": "chunk-one.example.com", "n": 2}
CHUNK_APPEND_PAYLOAD = {
    "email": "probe-chunked@example.com",
    "password": "secret",
    "status": "success",
    "recordedAt": "2026-01-05T00:00:00Z",
    "reason": "probe-chunked",
}
CHUNK_APPEND_EXPECTED = "2026-01-05T00:00:00Z\tprobe-chunked@example.com\tsecret\tsuccess\tprobe-chunked"
CHUNK_SYNC_PAYLOAD = {
    "records": [
        {
            "email": "probe-chunked-1@example.com",
            "password": "secret",
            "finalStatus": "success",
            "retryCount": 1,
        },
        {
            "email": "probe-chunked-2@example.com",
            "password": "secret",
            "finalStatus": "failed",
            "retryCount": 2,
        },
    ]
}
# --- L6 route-first dispatch probes ---------------------------------------
# Routing is resolved before any request body is parsed, and each view parses
# only the body it consumes.  The source technology parses the body before it
# dispatches, so a malformed body short-circuits *every* request into its
# malformed-JSON envelope.  These bodies are deliberately not valid JSON: an
# unterminated object, a blank body, and two bytes that are not valid UTF-8.
UNKNOWN_PATH = "/does-not-exist"
ROUTE_FIRST_BAD_JSON = BAD_JSON_BODY
ROUTE_FIRST_BLANK_BODY = "   "
ROUTE_FIRST_NON_UTF8_BODY = b"\xff\xfe"
MISSING_IMAP_ERROR = "Missing FLOWPILOT_CUSTOM_IMAP_USER/FLOWPILOT_CUSTOM_IMAP_PASS"
ACCOUNT_LOG_FILE = os.path.join("data", "account-run-history.txt")
ACCOUNT_SNAPSHOT_FILE = os.path.join("data", "account-run-history.json")
PUBLIC_ENV_KEYS = ("FLOWPILOT_SUB2API_REDIRECT_URI",)
# --- L7 request-envelope probes -------------------------------------------
# The request framing and a fixed body-size ceiling are validated before any
# matched route reads the body.  The source trusts the declared
# ``Content-Length`` verbatim and imposes no ceiling, so it neither rejects a
# conflicting ``Transfer-Encoding`` nor an unparseable length, and it happily
# parses an oversized body; the target rejects each of these with the documented
# envelope before routing and before any side effect.  ``Transfer-Encoding`` and
# the declared ``Content-Length`` are the only inputs the target may inspect: the
# probe never lets the body itself decide the outcome.
MAX_BODY_BYTES = 1024 * 1024
AMBIGUOUS_FRAMING_ERROR = "Ambiguous request framing"
INVALID_CONTENT_LENGTH_ERROR = "Invalid Content-Length"
BODY_TOO_LARGE_ERROR = "Request body too large"
FRAMING_CONFLICT_PAYLOAD = "{oops"
OVERSIZE_PAD_BYTES = MAX_BODY_BYTES + 4096
OVERSIZE_RANDOM_PAYLOAD = {"domain": "example.com", "n": 1, "pad": "x" * OVERSIZE_PAD_BYTES}
OVERSIZE_APPEND_PAYLOAD = {
    "email": "probe-oversize@example.com",
    "password": "secret",
    "status": "success",
    "reason": "probe-oversize",
    "pad": "x" * OVERSIZE_PAD_BYTES,
}
# --- L8 decoded-body ceiling and Transfer-Encoding acceptance -------------
# The ``MAX_BODY_BYTES`` ceiling is measured on the *decoded* entity body, and
# the only accepted ``Transfer-Encoding`` is a single ``chunked`` coding.  The
# source never inspects ``Transfer-Encoding`` and reads only ``Content-Length``,
# so it happily serves an oversized chunked body (and writes its side effect) and
# mis-handles every non-``chunked`` coding; the target answers the documented
# envelope before routing (for the coding set) or before the matched
# body-consuming route's work (for the decoded ceiling).  The probe builds
# exact-length JSON bodies so the boundary cases are deterministic.
UNSUPPORTED_TE_ERROR = "Unsupported Transfer-Encoding"
DECODED_EXACT_BYTES = MAX_BODY_BYTES
DECODED_OVER_BYTES = MAX_BODY_BYTES + 1
DECODED_OVER_APPEND_BYTES = MAX_BODY_BYTES + 4096
RANDOM_DB_FILE = os.path.join("data", "custom.sqlite3")
L8_RANDOM_OVER_PAYLOAD = {"domain": "example.com", "n": 1}
L8_RANDOM_SMALL_PAYLOAD = {"domain": "example.com", "n": 1}
L8_APPEND_OVER_PAYLOAD = {
    "email": "probe-l8-over@example.com",
    "password": "secret",
    "status": "success",
    "recordedAt": "2026-02-02T00:00:00Z",
    "reason": "probe-l8-over",
}
L8_APPEND_SMALL_PAYLOAD = {
    "email": "probe-l8-small@example.com",
    "password": "secret",
    "status": "success",
    "recordedAt": "2026-02-03T00:00:00Z",
    "reason": "probe-l8-small",
}
L8_APPEND_EXACT_PAYLOAD = {
    "email": "probe-l8-exact@example.com",
    "password": "secret",
    "status": "success",
    "recordedAt": "2026-02-01T00:00:00Z",
    "reason": "probe-l8-exact",
}
L8_APPEND_EXACT_EXPECTED = "2026-02-01T00:00:00Z\tprobe-l8-exact@example.com\tsecret\tsuccess\tprobe-l8-exact"
# --- L9 request-target byte fidelity --------------------------------------
# the unknown-path envelope echoes the request target exactly as it arrived on
# the wire.  The source decodes the raw request line with ISO-8859-1, so a raw
# (non-percent-encoded) octet such as ``0xC3`` survives as the single character
# ``U+00C3`` and ``0xA9`` as ``U+00A9``; a framework that re-encodes the target
# (or only exposes the percent-encoded ``RAW_URI``) loses that byte fidelity.
# The expected echo is therefore the raw octet string decoded as latin-1.
RAW_TARGET_UPPER = b"/caf\xc3\xa9"
RAW_TARGET_HIGH = b"/caf\xff"
RAW_TARGET_QUERY = b"/nope?q=caf\xc3\xa9"
RAW_TARGET_PERCENT = b"/%C3%A9"
# --- L9 zero-length entity and non-decimal Content-Length -----------------
# A matched body-consuming route that receives a zero-length entity (a declared
# ``Content-Length: 0``, an all-digit form that parses to zero such as ``00``,
# or no ``Content-Length`` header at all) MUST materialise the JSON payload as
# the empty object ``{}`` and then run its own field validation; it MUST NOT
# report a JSON parse failure.  An empty or whitespace-only ``Content-Length``
# value is not a decimal integer and MUST be answered with the framing envelope.
ZERO_LENGTH_RANDOM_ERROR = "Invalid domain"
ZERO_LENGTH_APPEND_ERROR = "Missing email/password/status for account log append"


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


def _raw_request(port, request_bytes, timeout=20.0):
    """Drive the helper over a raw socket so the request line stays verbatim.

    ``urllib`` normalises the request target, so the absolute-form probes must
    speak HTTP directly.  ``Connection: close`` makes the helper shut the socket
    once the response is written.  The response is read only up to its declared
    ``Content-Length``: an oversized request whose body the target rejects
    without draining must not make the probe wait for the peer to close.
    """
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        try:
            sock.sendall(request_bytes)
        except OSError:
            pass
        sock.settimeout(timeout)
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buffer += chunk
        head, _, body = buffer.partition(b"\r\n\r\n")
        content_length = None
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                try:
                    content_length = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    content_length = None
        if content_length is not None:
            while len(body) < content_length:
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                body += chunk
        sock.close()
    except Exception as exc:  # noqa: BLE001 - any failure is the observation
        return {"status": 0, "headers": {}, "body": "", "error": str(exc)}
    lines = head.split(b"\r\n")
    status = 0
    if lines and lines[0].startswith(b"HTTP/"):
        try:
            status = int(lines[0].split(b" ")[1])
        except (IndexError, ValueError):
            status = 0
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            key, value = line.split(b":", 1)
            headers[key.decode("latin-1").strip()] = value.decode("latin-1").strip()
    return {"status": status, "headers": headers, "body": body.decode("utf-8", errors="replace")}


def _raw_post(port, target, payload=b"{}"):
    body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    target_bytes = target if isinstance(target, bytes) else target.encode("utf-8")
    request = (
        b"POST " + target_bytes + b" HTTP/1.1\r\n"
        b"Host: 127.0.0.1:" + str(port).encode("ascii") + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )
    return _raw_request(port, request)


def _chunked_frames(payload, chunk_size=5):
    """Encode ``payload`` as an HTTP/1.1 ``chunked`` transfer body."""
    body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    if chunk_size < 1:
        chunk_size = 1
    frames = b""
    for index in range(0, len(body), chunk_size):
        piece = body[index:index + chunk_size]
        frames += ("%x\r\n" % len(piece)).encode("ascii") + piece + b"\r\n"
    frames += b"0\r\n\r\n"
    return frames


def _raw_chunked_post(port, target, payload=b"{}", chunk_size=5):
    """POST a ``Transfer-Encoding: chunked`` body over a raw socket.

    ``urllib`` always frames bodies with ``Content-Length``, so the chunked
    requests must speak HTTP directly.  ``Connection: close`` keeps the read
    deterministic once the response is written.
    """
    frames = _chunked_frames(payload, chunk_size)
    request = (
        b"POST " + target.encode("utf-8") + b" HTTP/1.1\r\n"
        b"Host: 127.0.0.1:" + str(port).encode("ascii") + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n"
        b"\r\n" + frames
    )
    return _raw_request(port, request)


def _raw_conflicting_framing_post(port, target, payload=FRAMING_CONFLICT_PAYLOAD):
    """POST with BOTH ``Content-Length`` and ``Transfer-Encoding: chunked``.

    The two framings disagree: the body is chunk-framed, while ``Content-Length``
    declares the length of the *frames*, not the payload.  A helper that trusts
    one framing and processes the body is following the source runtime's habit;
    the target must refuse to choose and answer the documented framing envelope.
    """
    frames = _chunked_frames(payload)
    request = (
        b"POST " + target.encode("utf-8") + b" HTTP/1.1\r\n"
        b"Host: 127.0.0.1:" + str(port).encode("ascii") + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(frames)).encode("ascii") + b"\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n"
        b"\r\n" + frames
    )
    return _raw_request(port, request)


def _raw_bad_length_post(port, target):
    """POST with a ``Content-Length`` header that is not a decimal integer."""
    request = (
        b"POST " + target.encode("utf-8") + b" HTTP/1.1\r\n"
        b"Host: 127.0.0.1:" + str(port).encode("ascii") + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: not-a-number\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    return _raw_request(port, request)


def _raw_oversize_post(port, target, payload):
    """POST a ``Content-Length`` body whose declared size exceeds the ceiling."""
    body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    request = (
        b"POST " + target.encode("utf-8") + b" HTTP/1.1\r\n"
        b"Host: 127.0.0.1:" + str(port).encode("ascii") + b"\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode("ascii") + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body
    )
    return _raw_request(port, request)


def _padded_json(payload, target_bytes):
    """Serialise ``payload`` (with an added ``pad`` string) to exactly N bytes.

    The ``pad`` value is a run of ASCII ``x`` characters, which JSON emits
    verbatim, so the encoded length is controlled byte for byte.  This lets the
    decoded-body boundary be probed at exactly ``MAX_BODY_BYTES`` and one byte
    over without depending on any particular base payload length.
    """
    payload = dict(payload)
    payload["pad"] = ""
    overhead = len(json.dumps(payload).encode("utf-8"))
    pad_length = target_bytes - overhead
    if pad_length < 0:
        raise ValueError("target body smaller than the base payload")
    payload["pad"] = "x" * pad_length
    body = json.dumps(payload).encode("utf-8")
    if len(body) != target_bytes:
        raise ValueError("padded body length mismatch")
    return body


def _raw_te_post(port, target, te_values, payload=b"{}", chunked=False, chunk_size=4096):
    """POST with one explicit ``Transfer-Encoding`` header line per value.

    ``te_values`` is a list of raw header values, emitted as separate header
    lines so that a repeated header (and a comma-carrying value) is observable on
    the wire exactly as written.  When ``chunked`` is true the payload is framed
    into HTTP/1.1 chunks; otherwise it is sent verbatim.  No ``Content-Length`` is
    ever added, so the framing error under test stays unambiguous.
    """
    body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
    frames = _chunked_frames(body, chunk_size) if chunked else body
    header_lines = b"".join(
        b"Transfer-Encoding: " + value.encode("latin-1") + b"\r\n" for value in te_values
    )
    request = (
        b"POST " + target.encode("utf-8") + b" HTTP/1.1\r\n"
        b"Host: 127.0.0.1:" + str(port).encode("ascii") + b"\r\n"
        b"Content-Type: application/json\r\n"
        + header_lines
        + b"Connection: close\r\n"
        b"\r\n" + frames
    )
    return _raw_request(port, request)


def _raw_length_post(port, target, length_value, payload=b"", method="POST", transfer_encoding=None):
    """POST with an explicit ``Content-Length`` value (or none at all).

    ``length_value`` is emitted verbatim after ``Content-Length: `` as raw
    bytes -- so ``b"0"``, ``b"00"``, ``b""`` and ``b"   "`` are all sent exactly
    as written -- while ``None`` omits the header entirely (a zero-length entity
    with no declared length).  The ``payload`` is sent verbatim after the blank
    line.  ``Connection: close`` keeps the read deterministic.
    """
    target_bytes = target if isinstance(target, bytes) else target.encode("utf-8")
    lines = [
        method.encode("ascii") + b" " + target_bytes + b" HTTP/1.1",
        b"Host: 127.0.0.1:" + str(port).encode("ascii"),
        b"Content-Type: application/json",
    ]
    if length_value is not None:
        lines.append(b"Content-Length: " + length_value)
    if transfer_encoding is not None:
        lines.append(b"Transfer-Encoding: " + transfer_encoding)
    lines.append(b"Connection: close")
    request = b"\r\n".join(lines) + b"\r\n\r\n" + payload
    return _raw_request(port, request)


def _collect_length_edges(port, body_path, empty_error):
    """Decimal lengths stay valid beyond Python's integer-conversion limit.

    Each header is well below the HTTP header-line limit. No entity bytes are
    sent for oversized declarations, which must be rejected before any read.
    """
    huge = b"9" * 5000
    zeros = b"0" * 5000
    cases = [
        ("huge_unknown", "/does-not-exist", huge, b"", "POST", None, 413, BODY_TOO_LARGE_ERROR),
        ("huge_unsupported_method", body_path, huge, b"", "PUT", None, 413, BODY_TOO_LARGE_ERROR),
        ("huge_head", body_path, huge, b"", "HEAD", None, 413, BODY_TOO_LARGE_ERROR),
        ("head_invalid", body_path, b"invalid", b"", "HEAD", None, 500, INVALID_CONTENT_LENGTH_ERROR),
        ("head_unsupported_te", body_path, None, b"", "HEAD", b"gzip", 500, UNSUPPORTED_TE_ERROR),
        ("head_ambiguous", body_path, b"0", b"", "HEAD", b"chunked", 500, AMBIGUOUS_FRAMING_ERROR),
        ("leading_zeros_nonempty", body_path, zeros + str(len(BAD_JSON_BODY)).encode("ascii"),
         BAD_JSON_BODY, "POST", None, 500, _source_invalid_json_message(BAD_JSON_BODY)),
        ("leading_zeros_empty", body_path, zeros, b"", "POST", None, 500, empty_error),
        ("huge_ambiguous", "/does-not-exist", huge, b"", "POST", b"gzip", 500, AMBIGUOUS_FRAMING_ERROR),
        ("huge_invalid", "/does-not-exist", huge + b"x", b"", "POST", None, 500, INVALID_CONTENT_LENGTH_ERROR),
    ]
    return [
        {"case": name, "expected_status": status, "expected_error": error,
         "response": _raw_length_post(port, path, length, body, method, encoding)}
        for name, path, length, body, method, encoding, status, error in cases
    ]


def _collect_unsupported_methods(port, target):
    # HTTP method names are case-sensitive: lower-case post/options do not
    # become supported merely because Flask normalizes request.method.
    cases = [
        {"case": method, "expected_status": 404,
         "expected_error": "Unsupported path: " + target,
         "response": _raw_length_post(port, target, str(len(BAD_JSON_BODY)).encode("ascii"),
                                       BAD_JSON_BODY, method=method)}
        for method in ("GET", "HEAD", "PUT", "PATCH", "DELETE", "post", "options")
    ]
    # The task explicitly requires the JSON envelope for every unsupported
    # method. A framework's default suppression of HEAD response bodies must
    # not silently turn that envelope into an empty entity, on any path.
    cases.append({
        "case": "HEAD_unknown", "expected_status": 404,
        "expected_error": "Unsupported path: /does-not-exist?head=1",
        "response": _raw_length_post(port, "/does-not-exist?head=1", b"0", method="HEAD"),
    })
    return cases


def _collect_unknown_paths(port, documented_path):
    # Decoding /%2fhealth yields //health, not the documented /health. Neither
    # the HTTP server nor Flask may turn that distinct path into a served alias.
    targets = ["/%2f" + documented_path.lstrip("/"), "/" + documented_path]
    return [
        {"case": target, "expected_status": 404,
         "expected_error": "Unsupported path: " + target,
         "response": _raw_length_post(port, target, str(len(BAD_JSON_BODY)).encode("ascii"),
                                       BAD_JSON_BODY)}
        for target in targets
    ]


def _envelope_failures(cases):
    failures = []
    for case in cases:
        response = case["response"]
        if not _error_envelope(response, case["expected_status"], case["expected_error"]):
            failures.append({"case": case["case"], "response": response})
    return failures


def _read_text_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _random_db_count(work_root):
    """Row count of ``custom_mail_helper``'s ``generated_emails`` table.

    The probe points the helper at a scratch SQLite file inside the work tree, so
    the count can be read back read-only to prove a rejected request inserted
    nothing.  A missing file means zero rows; any read failure yields ``-1`` so it
    never accidentally equals a real count.
    """
    path = os.path.join(work_root, RANDOM_DB_FILE)
    if not os.path.exists(path):
        return 0
    try:
        connection = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
        try:
            return int(connection.execute("SELECT COUNT(*) FROM generated_emails").fetchone()[0])
        finally:
            connection.close()
    except Exception:  # noqa: BLE001 - an unreadable table is a failed observation
        return -1


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


def _source_invalid_json_message(raw):
    """The exact message the source technology emits for a malformed JSON body.

    The source decodes the request body and calls ``json.loads``; the failure
    envelope echoes ``Invalid JSON payload: <exception>``.  Deriving the
    expected detail from the running interpreter keeps this observation
    independent of the Python minor version (the candidate helper and this probe
    always share the same interpreter inside the verifier container).
    """
    try:
        json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any failure is the observation
        return "Invalid JSON payload: %s" % (exc,)
    return None


def _payload_error(response):
    body = _json_body(response)
    return body.get("error") if isinstance(body, dict) else None


def _collect(work_root):
    """Run every concrete probe once and return the raw observation bundle."""
    bundle = {"work_root": work_root}
    bad_json_expected = _source_invalid_json_message(BAD_JSON_BODY)
    bundle["bad_json_expected"] = bad_json_expected

    hotmail = {}
    with HelperServer(work_root, "hotmail_helper.py", HOTMAIL_PORT) as server:
        log_path = os.path.join(work_root, ACCOUNT_LOG_FILE)
        snapshot_path = os.path.join(work_root, ACCOUNT_SNAPSHOT_FILE)
        hotmail["options_served"] = server.call("OPTIONS", "/messages", content_type=None)
        hotmail["options_unknown"] = server.call("OPTIONS", "/does-not-exist", content_type=None)
        hotmail["unknown_query"] = server.call("POST", "/does-not-exist?a=1&b=2", "{}")
        hotmail["unknown_percent"] = server.call("POST", "/%E4%B8%AD", "{}")
        hotmail["unknown_trailing_slash"] = server.call("POST", "/messages/", "{}")
        hotmail["bad_json"] = server.call("POST", BAD_JSON_PATH_HOTMAIL, BAD_JSON_BODY)
        hotmail["append_query"] = server.call("POST", QUERY_APPEND, json.dumps(APPEND_QUERY_PAYLOAD))
        hotmail["append_query_log"] = _read_text_file(log_path)
        hotmail["sync_query"] = server.call("POST", QUERY_SYNC, json.dumps(SYNC_QUERY_PAYLOAD))
        hotmail["sync_query_snapshot"] = _read_json_file(snapshot_path)
        hotmail["sync_encoded"] = server.call("POST", ENCODED_SYNC, json.dumps(SYNC_ENCODED_PAYLOAD))
        hotmail["sync_encoded_snapshot"] = _read_json_file(snapshot_path)
        hotmail["absform_append"] = _raw_post(
            HOTMAIL_PORT,
            "http://127.0.0.1:%d/append-account-log" % HOTMAIL_PORT,
            json.dumps(APPEND_ABSOLUTE_PAYLOAD),
        )
        hotmail["absform_append_log"] = _read_text_file(log_path)
        hotmail["chunked_append"] = _raw_chunked_post(
            HOTMAIL_PORT, "/append-account-log", json.dumps(CHUNK_APPEND_PAYLOAD)
        )
        hotmail["chunked_append_log"] = _read_text_file(log_path)
        hotmail["chunked_sync"] = _raw_chunked_post(
            HOTMAIL_PORT, "/sync-account-run-records", json.dumps(CHUNK_SYNC_PAYLOAD)
        )
        hotmail["chunked_sync_snapshot"] = _read_json_file(snapshot_path)
        hotmail["chunked_bad_json"] = _raw_chunked_post(
            HOTMAIL_PORT, BAD_JSON_PATH_HOTMAIL, BAD_JSON_BODY
        )
        hotmail["unknown_bad_json"] = server.call("POST", UNKNOWN_PATH, ROUTE_FIRST_BAD_JSON)
        hotmail["framing_conflict_append_log_before"] = _read_text_file(log_path)
        hotmail["framing_conflict_append"] = _raw_conflicting_framing_post(
            HOTMAIL_PORT, "/append-account-log"
        )
        hotmail["framing_conflict_append_log_after"] = _read_text_file(log_path)
        hotmail["bad_length_messages"] = _raw_bad_length_post(HOTMAIL_PORT, "/messages")
        hotmail["oversize_append_log_before"] = _read_text_file(log_path)
        hotmail["oversize_append"] = _raw_oversize_post(
            HOTMAIL_PORT, "/append-account-log", json.dumps(OVERSIZE_APPEND_PAYLOAD)
        )
        hotmail["oversize_append_log_after"] = _read_text_file(log_path)
        hotmail["l8_chunked_over_log_before"] = _read_text_file(log_path)
        hotmail["l8_chunked_over_append"] = _raw_te_post(
            HOTMAIL_PORT,
            "/append-account-log",
            ["chunked"],
            _padded_json(L8_APPEND_OVER_PAYLOAD, DECODED_OVER_APPEND_BYTES),
            chunked=True,
        )
        hotmail["l8_chunked_over_log_after"] = _read_text_file(log_path)
        hotmail["l8_chunked_exact_log_before"] = _read_text_file(log_path)
        hotmail["l8_chunked_exact_append"] = _raw_te_post(
            HOTMAIL_PORT,
            "/append-account-log",
            ["chunked"],
            _padded_json(L8_APPEND_EXACT_PAYLOAD, DECODED_EXACT_BYTES),
            chunked=True,
        )
        hotmail["l8_chunked_exact_log_after"] = _read_text_file(log_path)
        hotmail["l8_two_te_log_before"] = _read_text_file(log_path)
        hotmail["l8_two_te_append"] = _raw_te_post(
            HOTMAIL_PORT,
            "/append-account-log",
            ["chunked", "gzip"],
            _padded_json(L8_APPEND_SMALL_PAYLOAD, 256),
            chunked=True,
        )
        hotmail["l8_two_te_log_after"] = _read_text_file(log_path)
        # L9: a matched body-consuming route with a zero-length entity routes as
        # {}, and a whitespace-only Content-Length is answered as invalid.  Both
        # are framed before the route writes anything, so the account log gains
        # no line.
        hotmail["l9_zero_length_log_before"] = _read_text_file(log_path)
        hotmail["l9_zero_length_append"] = _raw_length_post(
            HOTMAIL_PORT, "/append-account-log", None
        )
        hotmail["l9_zero_length_log_after"] = _read_text_file(log_path)
        hotmail["l9_ws_length_log_before"] = _read_text_file(log_path)
        hotmail["l9_ws_length_append"] = _raw_length_post(
            HOTMAIL_PORT, "/append-account-log", b"   "
        )
        hotmail["l9_ws_length_log_after"] = _read_text_file(log_path)
        # L9: a raw non-UTF-8 target byte is echoed with latin-1 fidelity.
        hotmail["l9_raw_target_high"] = _raw_post(HOTMAIL_PORT, RAW_TARGET_HIGH)
        hotmail["length_edges"] = _collect_length_edges(
            HOTMAIL_PORT, "/append-account-log", ZERO_LENGTH_APPEND_ERROR
        )
        hotmail["unsupported_methods"] = _collect_unsupported_methods(HOTMAIL_PORT, "/messages?x=1")
        hotmail["unknown_paths"] = _collect_unknown_paths(HOTMAIL_PORT, "/messages")
    bundle["hotmail"] = hotmail

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
        custom["options_served"] = server.call("OPTIONS", "/messages", content_type=None)
        custom["unknown_percent"] = server.call("POST", "/%E4%B8%AD", "{}")
        custom["bad_json"] = server.call("POST", BAD_JSON_PATH_CUSTOM, BAD_JSON_BODY)
        custom["health_query"] = server.call("POST", QUERY_HEALTH, "{}")
        custom["random_query"] = server.call("POST", QUERY_RANDOM, json.dumps(RANDOM_EMAIL_PAYLOAD))
        custom["env_query"] = server.call("POST", QUERY_ENV, "{}")
        custom["messages_query"] = server.call("POST", QUERY_MESSAGES, "{}")
        custom["health_encoded"] = server.call("POST", ENCODED_HEALTH, "{}")
        custom["random_encoded"] = server.call("POST", ENCODED_RANDOM, json.dumps(RANDOM_EMAIL_PAYLOAD))
        custom["absform_health"] = _raw_post(CUSTOM_PORT, "http://127.0.0.1:%d/health" % CUSTOM_PORT)
        custom["absform_health_query"] = _raw_post(CUSTOM_PORT, "http://127.0.0.1:%d/health?a=1" % CUSTOM_PORT)
        custom["chunked_random"] = _raw_chunked_post(
            CUSTOM_PORT, "/random-email", json.dumps(CHUNK_RANDOM_PAYLOAD)
        )
        custom["chunked_random_one"] = _raw_chunked_post(
            CUSTOM_PORT, "/random-email", json.dumps(CHUNK_RANDOM_ONE_PAYLOAD), chunk_size=1
        )
        custom["chunked_bad_json"] = _raw_chunked_post(
            CUSTOM_PORT, "/random-email", BAD_JSON_BODY
        )
        custom["unknown_bad_json"] = server.call("POST", UNKNOWN_PATH, ROUTE_FIRST_BAD_JSON)
        custom["health_bad_json"] = server.call("POST", "/health", ROUTE_FIRST_BAD_JSON)
        custom["health_blank"] = server.call("POST", "/health", ROUTE_FIRST_BLANK_BODY)
        custom["env_bad_json"] = server.call("POST", "/env", ROUTE_FIRST_BAD_JSON)
        custom["env_non_utf8"] = server.call("POST", "/env", ROUTE_FIRST_NON_UTF8_BODY)
        custom["framing_conflict_health"] = _raw_conflicting_framing_post(CUSTOM_PORT, "/health")
        custom["framing_conflict_random"] = _raw_conflicting_framing_post(
            CUSTOM_PORT, "/random-email"
        )
        custom["bad_length_env"] = _raw_bad_length_post(CUSTOM_PORT, "/env")
        custom["oversize_random"] = _raw_oversize_post(
            CUSTOM_PORT, "/random-email", json.dumps(OVERSIZE_RANDOM_PAYLOAD)
        )
        custom["l8_chunked_over_db_before"] = _random_db_count(work_root)
        custom["l8_chunked_over_random"] = _raw_te_post(
            CUSTOM_PORT,
            "/random-email",
            ["chunked"],
            _padded_json(L8_RANDOM_OVER_PAYLOAD, DECODED_OVER_BYTES),
            chunked=True,
        )
        custom["l8_chunked_over_db_after"] = _random_db_count(work_root)
        custom["l8_te_gzip_db_before"] = _random_db_count(work_root)
        custom["l8_te_gzip_random"] = _raw_te_post(
            CUSTOM_PORT, "/random-email", ["gzip"], b"{}"
        )
        custom["l8_te_gzip_db_after"] = _random_db_count(work_root)
        custom["l8_te_chunked_gzip_random"] = _raw_te_post(
            CUSTOM_PORT,
            "/random-email",
            ["chunked, gzip"],
            _padded_json(L8_RANDOM_SMALL_PAYLOAD, 256),
            chunked=True,
        )
        custom["l8_te_identity_health"] = _raw_te_post(
            CUSTOM_PORT, "/health", ["identity"], b"{}"
        )
        custom["l8_te_case_random"] = _raw_te_post(
            CUSTOM_PORT,
            "/random-email",
            ["  Chunked "],
            _padded_json(L8_RANDOM_SMALL_PAYLOAD, 256),
            chunked=True,
        )
        custom["l8_te_gzip_unknown"] = _raw_te_post(
            CUSTOM_PORT, "/does-not-exist", ["gzip"], b"{}"
        )
        # L9: raw request-target bytes keep the source's latin-1 fidelity.
        custom["l9_raw_target_upper"] = _raw_post(CUSTOM_PORT, RAW_TARGET_UPPER)
        custom["l9_raw_target_query"] = _raw_post(CUSTOM_PORT, RAW_TARGET_QUERY)
        custom["l9_percent_target"] = _raw_post(CUSTOM_PORT, RAW_TARGET_PERCENT)
        # L9: a zero-length entity is routed as {} (Content-Length: 0 and 00),
        # the route's own validation fails, and no random address is inserted.
        custom["l9_zero_length_random_db_before"] = _random_db_count(work_root)
        custom["l9_zero_length_random"] = _raw_length_post(
            CUSTOM_PORT, "/random-email", b"0"
        )
        custom["l9_zero_length_random_db_after"] = _random_db_count(work_root)
        custom["l9_zero_padded_random_db_before"] = _random_db_count(work_root)
        custom["l9_zero_padded_random"] = _raw_length_post(
            CUSTOM_PORT, "/random-email", b"00"
        )
        custom["l9_zero_padded_random_db_after"] = _random_db_count(work_root)
        # L9: an empty Content-Length value is not a decimal integer; it is
        # rejected before routing, so no random address is inserted.
        custom["l9_empty_length_random_db_before"] = _random_db_count(work_root)
        custom["l9_empty_length_random"] = _raw_length_post(
            CUSTOM_PORT, "/random-email", b""
        )
        custom["l9_empty_length_random_db_after"] = _random_db_count(work_root)
        custom["length_edges"] = _collect_length_edges(
            CUSTOM_PORT, "/random-email", ZERO_LENGTH_RANDOM_ERROR
        )
        custom["unsupported_methods"] = _collect_unsupported_methods(CUSTOM_PORT, "/health?x=1")
        custom["unknown_paths"] = _collect_unknown_paths(CUSTOM_PORT, "/health")
    bundle["custom"] = custom

    return bundle


def _preflight_predicate(response):
    return (
        response.get("status") == 204
        and _cors_ok(response)
        and _json_charset(response)
        and _served_by_target(response)
        and response.get("body") == ""
    )


def _served_ok(response):
    return (
        response.get("status") == 200
        and _served_by_target(response)
        and _cors_ok(response)
        and _json_charset(response)
        and _json_body(response).get("ok") is True
    )


def _env_ok(response):
    env = _json_body(response).get("env")
    return _served_ok(response) and isinstance(env, dict) and set(env.keys()) == set(PUBLIC_ENV_KEYS)


def _random_ok(response):
    body = _json_body(response)
    emails = body.get("emails")
    return (
        _served_ok(response)
        and isinstance(emails, list)
        and len(emails) == 1
        and emails[0] == body.get("email")
        and str(emails[0]).endswith("@example.com")
    )


def _random_count_ok(response, count, domain_suffix):
    """A served random-email response with exactly ``count`` addresses."""
    body = _json_body(response)
    emails = body.get("emails")
    return (
        _served_ok(response)
        and body.get("count") == count
        and isinstance(emails, list)
        and len(emails) == count
        and all(str(item).endswith(domain_suffix) for item in emails)
    )


def _error_envelope(response, status, error):
    """Compare the contractual UTF-8 JSON serialization, not just its values.

    Whitespace, literal non-ASCII characters, key order and the lack of a
    trailing newline are observable parts of every failure/404 response.
    """
    expected = json.dumps({"ok": False, "error": error}, ensure_ascii=False)
    return (
        response.get("status") == status
        and _served_by_target(response)
        and _cors_ok(response)
        and _json_charset(response)
        and response.get("body") == expected
    )


def _malformed_json_failure(response, expected):
    """HTTP 500 JSON envelope whose error is the source parse-failure text."""
    return expected is not None and _error_envelope(response, 500, expected)


def _unknown_path_error(response, target):
    """The served unknown-path 404 envelope echoes ``target`` verbatim."""
    return _error_envelope(response, 404, "Unsupported path: %s" % (target,))


def _missing_imap(response):
    return _error_envelope(response, 500, MISSING_IMAP_ERROR)


def _framing_envelope(response, error):
    """HTTP 500 served JSON envelope carrying the documented framing error."""
    return _error_envelope(response, 500, error)


def _body_too_large(response):
    """HTTP 413 served JSON envelope carrying the documented size error."""
    return _error_envelope(response, 413, BODY_TOO_LARGE_ERROR)


def _unsupported_te(response):
    """HTTP 500 served JSON envelope carrying the documented coding error."""
    return _error_envelope(response, 500, UNSUPPORTED_TE_ERROR)


def _line_count(text):
    return len([line for line in (text or "").splitlines() if line.strip()])


def _append_has(response, log_text, expected):
    lines = [line for line in (log_text or "").splitlines() if line.strip()]
    return _served_ok(response) and expected in lines


def _sync_total(response, snapshot, total):
    if not _served_ok(response) or not isinstance(snapshot, dict):
        return False
    summary = snapshot.get("summary")
    records = snapshot.get("records")
    return (
        isinstance(summary, dict)
        and summary.get("total") == total
        and isinstance(records, list)
        and len(records) == total
    )


def _evaluate(bundle):
    """Map each F2P observation id to (passed, detail) using the raw bundle."""
    results = {}

    def record(observation_id, passed, detail):
        results[observation_id] = {"passed": bool(passed), "detail": detail}

    hot = bundle["hotmail"]
    cus = bundle["custom"]
    bad_json_expected = bundle.get("bad_json_expected")

    # F2P-073: hotmail preflight is a served JSON 204 envelope.
    record(
        "F2P-073",
        _preflight_predicate(hot["options_served"]),
        "status=%s ct=%s server=%s"
        % (hot["options_served"]["status"], hot["options_served"]["headers"].get("Content-Type"),
           hot["options_served"]["headers"].get("Server")),
    )
    # F2P-074: hotmail preflight on an unknown path keeps the JSON envelope.
    record(
        "F2P-074",
        _preflight_predicate(hot["options_unknown"]),
        "status=%s ct=%s server=%s"
        % (hot["options_unknown"]["status"], hot["options_unknown"]["headers"].get("Content-Type"),
           hot["options_unknown"]["headers"].get("Server")),
    )
    # F2P-075: custom preflight is a served JSON 204 envelope.
    record(
        "F2P-075",
        _preflight_predicate(cus["options_served"]),
        "status=%s ct=%s server=%s"
        % (cus["options_served"]["status"], cus["options_served"]["headers"].get("Content-Type"),
           cus["options_served"]["headers"].get("Server")),
    )
    # F2P-076: unknown-path 404 echoes the original request target including query.
    record(
        "F2P-076",
        _unknown_path_error(hot["unknown_query"], "/does-not-exist?a=1&b=2"),
        "status=%s error=%r" % (hot["unknown_query"]["status"], _payload_error(hot["unknown_query"])),
    )
    # F2P-077: unknown-path 404 echoes percent-encoded bytes verbatim (hotmail).
    record(
        "F2P-077",
        _unknown_path_error(hot["unknown_percent"], "/%E4%B8%AD"),
        "status=%s error=%r" % (hot["unknown_percent"]["status"], _payload_error(hot["unknown_percent"])),
    )
    # F2P-078: unknown-path 404 echoes percent-encoded bytes verbatim (custom).
    record(
        "F2P-078",
        _unknown_path_error(cus["unknown_percent"], "/%E4%B8%AD"),
        "status=%s error=%r" % (cus["unknown_percent"]["status"], _payload_error(cus["unknown_percent"])),
    )
    # F2P-079: unknown-path 404 echoes a trailing-slash target verbatim.
    record(
        "F2P-079",
        _unknown_path_error(hot["unknown_trailing_slash"], "/messages/"),
        "status=%s error=%r" % (hot["unknown_trailing_slash"]["status"], _payload_error(hot["unknown_trailing_slash"])),
    )
    # F2P-080: malformed JSON reproduces the source parse failure verbatim (hotmail).
    hot_error = _payload_error(hot["bad_json"])
    record(
        "F2P-080",
        _malformed_json_failure(hot["bad_json"], bad_json_expected),
        "status=%s error=%r expected=%r"
        % (hot["bad_json"]["status"], hot_error, bad_json_expected),
    )
    # F2P-081: malformed JSON reproduces the source parse failure verbatim (custom).
    cus_error = _payload_error(cus["bad_json"])
    record(
        "F2P-081",
        _malformed_json_failure(cus["bad_json"], bad_json_expected),
        "status=%s error=%r expected=%r"
        % (cus["bad_json"]["status"], cus_error, bad_json_expected),
    )
    # --- L4 routing: the request target's query string never routes ---------
    # F2P-082: custom /health is served even with a trailing query string.
    record(
        "F2P-082",
        _served_ok(cus["health_query"]),
        "status=%s server=%s body=%r"
        % (cus["health_query"]["status"], cus["health_query"]["headers"].get("Server"),
           cus["health_query"]["body"][:80]),
    )
    # F2P-083: custom /random-email serves a query-carrying request.
    record(
        "F2P-083",
        _random_ok(cus["random_query"]),
        "status=%s server=%s body=%r"
        % (cus["random_query"]["status"], cus["random_query"]["headers"].get("Server"),
           cus["random_query"]["body"][:80]),
    )
    # F2P-084: custom /env serves a query-carrying request (public key set only).
    record(
        "F2P-084",
        _env_ok(cus["env_query"]),
        "status=%s server=%s body=%r"
        % (cus["env_query"]["status"], cus["env_query"]["headers"].get("Server"),
           cus["env_query"]["body"][:80]),
    )
    # F2P-085: custom /messages routes by path, ignoring the query string; the
    # handler fails before any IMAP I/O on the missing credentials envelope.
    record(
        "F2P-085",
        _missing_imap(cus["messages_query"]),
        "status=%s error=%r" % (cus["messages_query"]["status"], _payload_error(cus["messages_query"])),
    )
    # F2P-086: hotmail /append-account-log writes its row for a query target.
    record(
        "F2P-086",
        _append_has(hot["append_query"], hot["append_query_log"], APPEND_QUERY_EXPECTED),
        "status=%s log=%r" % (hot["append_query"]["status"], (hot["append_query_log"] or "")[:80]),
    )
    # F2P-087: hotmail /sync-account-run-records writes the snapshot for a query target.
    record(
        "F2P-087",
        _sync_total(hot["sync_query"], hot["sync_query_snapshot"], 1),
        "status=%s snapshot=%r"
        % (hot["sync_query"]["status"], json.dumps(hot["sync_query_snapshot"], ensure_ascii=False)[:90]),
    )
    # --- L4 routing: percent-encoded route bytes decode before matching -----
    # F2P-088: custom /he%61lth decodes to /health and is served.
    record(
        "F2P-088",
        _served_ok(cus["health_encoded"]),
        "status=%s server=%s body=%r"
        % (cus["health_encoded"]["status"], cus["health_encoded"]["headers"].get("Server"),
           cus["health_encoded"]["body"][:80]),
    )
    # F2P-089: custom /random-%65mail decodes to /random-email and is served.
    record(
        "F2P-089",
        _random_ok(cus["random_encoded"]),
        "status=%s server=%s body=%r"
        % (cus["random_encoded"]["status"], cus["random_encoded"]["headers"].get("Server"),
           cus["random_encoded"]["body"][:80]),
    )
    # F2P-090: hotmail /sync-account-run-%72ecords decodes to the sync route.
    record(
        "F2P-090",
        _sync_total(hot["sync_encoded"], hot["sync_encoded_snapshot"], 2),
        "status=%s snapshot=%r"
        % (hot["sync_encoded"]["status"], json.dumps(hot["sync_encoded_snapshot"], ensure_ascii=False)[:90]),
    )
    # --- L4 routing: absolute-form request targets route by path component ---
    # F2P-091: custom absolute-form POST http://host/health is served.
    record(
        "F2P-091",
        _served_ok(cus["absform_health"]),
        "status=%s server=%s body=%r"
        % (cus["absform_health"]["status"], cus["absform_health"]["headers"].get("Server"),
           cus["absform_health"]["body"][:80]),
    )
    # F2P-092: custom absolute-form target with a query string is served.
    record(
        "F2P-092",
        _served_ok(cus["absform_health_query"]),
        "status=%s server=%s body=%r"
        % (cus["absform_health_query"]["status"], cus["absform_health_query"]["headers"].get("Server"),
           cus["absform_health_query"]["body"][:80]),
    )
    # F2P-093: hotmail absolute-form POST http://host/append-account-log writes its row.
    record(
        "F2P-093",
        _append_has(hot["absform_append"], hot["absform_append_log"], APPEND_ABSOLUTE_EXPECTED),
        "status=%s log=%r" % (hot["absform_append"]["status"], (hot["absform_append_log"] or "")[:80]),
    )
    # --- L5 transfer encoding: chunked bodies parse like Content-Length bodies ---
    # F2P-094: custom /random-email serves a chunked valid body (5-byte frames).
    record(
        "F2P-094",
        _random_count_ok(cus["chunked_random"], 3, "@chunked.example.com"),
        "status=%s server=%s body=%r"
        % (cus["chunked_random"]["status"], cus["chunked_random"]["headers"].get("Server"),
           cus["chunked_random"]["body"][:90]),
    )
    # F2P-095: the same holds with single-byte chunk frames.
    record(
        "F2P-095",
        _random_count_ok(cus["chunked_random_one"], 2, "@chunk-one.example.com"),
        "status=%s server=%s body=%r"
        % (cus["chunked_random_one"]["status"], cus["chunked_random_one"]["headers"].get("Server"),
           cus["chunked_random_one"]["body"][:90]),
    )
    # F2P-096: hotmail /append-account-log writes its row for a chunked body.
    record(
        "F2P-096",
        _append_has(hot["chunked_append"], hot["chunked_append_log"], CHUNK_APPEND_EXPECTED),
        "status=%s log=%r" % (hot["chunked_append"]["status"], (hot["chunked_append_log"] or "")[:90]),
    )
    # F2P-097: hotmail /sync-account-run-records writes the snapshot for a chunked body.
    record(
        "F2P-097",
        _sync_total(hot["chunked_sync"], hot["chunked_sync_snapshot"], 2),
        "status=%s snapshot_total=%s"
        % (hot["chunked_sync"]["status"],
           (hot["chunked_sync_snapshot"] or {}).get("summary", {}).get("total")
           if isinstance(hot["chunked_sync_snapshot"], dict) else None),
    )
    # F2P-098: a chunked malformed body fails with the source parse-failure text (custom).
    record(
        "F2P-098",
        _malformed_json_failure(cus["chunked_bad_json"], bad_json_expected),
        "status=%s error=%r expected=%r"
        % (cus["chunked_bad_json"]["status"], _payload_error(cus["chunked_bad_json"]), bad_json_expected),
    )
    # F2P-099: a chunked malformed body fails with the source parse-failure text (hotmail).
    record(
        "F2P-099",
        _malformed_json_failure(hot["chunked_bad_json"], bad_json_expected),
        "status=%s error=%r expected=%r"
        % (hot["chunked_bad_json"]["status"], _payload_error(hot["chunked_bad_json"]), bad_json_expected),
    )
    # --- L6 route-first dispatch: routing precedes body parsing --------------
    # F2P-100/101: unknown paths and unsupported methods precede body parsing.
    cus_method_failures = _envelope_failures(cus["unsupported_methods"] + cus["unknown_paths"])
    hot_method_failures = _envelope_failures(hot["unsupported_methods"] + hot["unknown_paths"])
    record(
        "F2P-100",
        _unknown_path_error(cus["unknown_bad_json"], UNKNOWN_PATH) and not cus_method_failures,
        "status=%s server=%s error=%r unsupported_method_failures=%r"
        % (cus["unknown_bad_json"]["status"], cus["unknown_bad_json"]["headers"].get("Server"),
           _payload_error(cus["unknown_bad_json"]), cus_method_failures),
    )
    # F2P-101: an unknown path answers the 404 envelope even for a malformed body (hotmail).
    record(
        "F2P-101",
        _unknown_path_error(hot["unknown_bad_json"], UNKNOWN_PATH) and not hot_method_failures,
        "status=%s server=%s error=%r unsupported_method_failures=%r"
        % (hot["unknown_bad_json"]["status"], hot["unknown_bad_json"]["headers"].get("Server"),
           _payload_error(hot["unknown_bad_json"]), hot_method_failures),
    )
    # F2P-102: custom /health does not consume the body, so a malformed body still succeeds.
    record(
        "F2P-102",
        _served_ok(cus["health_bad_json"]),
        "status=%s server=%s body=%r"
        % (cus["health_bad_json"]["status"], cus["health_bad_json"]["headers"].get("Server"),
           cus["health_bad_json"]["body"][:80]),
    )
    # F2P-103: custom /health succeeds for a blank body as well.
    record(
        "F2P-103",
        _served_ok(cus["health_blank"]),
        "status=%s server=%s body=%r"
        % (cus["health_blank"]["status"], cus["health_blank"]["headers"].get("Server"),
           cus["health_blank"]["body"][:80]),
    )
    # F2P-104: custom /env does not consume the body, so a malformed body still succeeds.
    record(
        "F2P-104",
        _env_ok(cus["env_bad_json"]),
        "status=%s server=%s body=%r"
        % (cus["env_bad_json"]["status"], cus["env_bad_json"]["headers"].get("Server"),
           cus["env_bad_json"]["body"][:80]),
    )
    # F2P-105: custom /env succeeds for a body that is not valid UTF-8.
    record(
        "F2P-105",
        _env_ok(cus["env_non_utf8"]),
        "status=%s server=%s body=%r"
        % (cus["env_non_utf8"]["status"], cus["env_non_utf8"]["headers"].get("Server"),
           cus["env_non_utf8"]["body"][:80]),
    )
    # --- L7 request envelope: framing and body-size validation --------------
    # F2P-106: custom /health rejects a request carrying both framings; the
    # route does not consume the body, so the rejection must happen up front.
    record(
        "F2P-106",
        _framing_envelope(cus["framing_conflict_health"], AMBIGUOUS_FRAMING_ERROR),
        "status=%s server=%s error=%r"
        % (cus["framing_conflict_health"]["status"],
           cus["framing_conflict_health"]["headers"].get("Server"),
           _payload_error(cus["framing_conflict_health"])),
    )
    # F2P-107: hotmail /append-account-log rejects the same conflict and appends
    # no line, so the request never reaches the route or its side effect.
    record(
        "F2P-107",
        _framing_envelope(hot["framing_conflict_append"], AMBIGUOUS_FRAMING_ERROR)
        and hot["framing_conflict_append_log_before"] == hot["framing_conflict_append_log_after"],
        "status=%s server=%s error=%r log_delta=%d"
        % (hot["framing_conflict_append"]["status"],
           hot["framing_conflict_append"]["headers"].get("Server"),
           _payload_error(hot["framing_conflict_append"]),
           len(hot["framing_conflict_append_log_after"] or "")
           - len(hot["framing_conflict_append_log_before"] or "")),
    )
    # F2P-108: custom /random-email rejects the conflicting framing.
    record(
        "F2P-108",
        _framing_envelope(cus["framing_conflict_random"], AMBIGUOUS_FRAMING_ERROR),
        "status=%s server=%s error=%r"
        % (cus["framing_conflict_random"]["status"],
           cus["framing_conflict_random"]["headers"].get("Server"),
           _payload_error(cus["framing_conflict_random"])),
    )
    # F2P-109/110: Content-Length syntax, numeric size and leading-zero handling.
    hot_length_failures = _envelope_failures(hot["length_edges"])
    cus_length_failures = _envelope_failures(cus["length_edges"])
    record(
        "F2P-109",
        _framing_envelope(hot["bad_length_messages"], INVALID_CONTENT_LENGTH_ERROR)
        and not hot_length_failures,
        "status=%s server=%s error=%r length_edge_failures=%r"
        % (hot["bad_length_messages"]["status"],
           hot["bad_length_messages"]["headers"].get("Server"),
           _payload_error(hot["bad_length_messages"]), hot_length_failures),
    )
    # F2P-110: custom /env rejects a Content-Length that is not a number.
    record(
        "F2P-110",
        _framing_envelope(cus["bad_length_env"], INVALID_CONTENT_LENGTH_ERROR)
        and not cus_length_failures,
        "status=%s server=%s error=%r length_edge_failures=%r"
        % (cus["bad_length_env"]["status"],
           cus["bad_length_env"]["headers"].get("Server"),
           _payload_error(cus["bad_length_env"]), cus_length_failures),
    )
    # F2P-111: custom /random-email rejects a body over the fixed ceiling.
    record(
        "F2P-111",
        _body_too_large(cus["oversize_random"]),
        "status=%s server=%s error=%r"
        % (cus["oversize_random"]["status"],
           cus["oversize_random"]["headers"].get("Server"),
           _payload_error(cus["oversize_random"])),
    )
    # F2P-112: hotmail /append-account-log rejects an oversized body and appends
    # no line, so the rejection precedes routing and the write side effect.
    record(
        "F2P-112",
        _body_too_large(hot["oversize_append"])
        and hot["oversize_append_log_before"] == hot["oversize_append_log_after"],
        "status=%s server=%s error=%r log_delta=%d"
        % (hot["oversize_append"]["status"],
           hot["oversize_append"]["headers"].get("Server"),
           _payload_error(hot["oversize_append"]),
           len(hot["oversize_append_log_after"] or "")
           - len(hot["oversize_append_log_before"] or "")),
    )
    # --- L8 decoded-body ceiling: the 1 MiB limit covers chunked bodies -------
    # F2P-113: custom /random-email rejects a chunked body that decodes to one
    # byte over the ceiling and inserts no random address.
    record(
        "F2P-113",
        _body_too_large(cus["l8_chunked_over_random"])
        and cus["l8_chunked_over_db_before"] == cus["l8_chunked_over_db_after"],
        "status=%s server=%s error=%r rows=%s->%s"
        % (cus["l8_chunked_over_random"]["status"],
           cus["l8_chunked_over_random"]["headers"].get("Server"),
           _payload_error(cus["l8_chunked_over_random"]),
           cus["l8_chunked_over_db_before"], cus["l8_chunked_over_db_after"]),
    )
    # F2P-114: hotmail /append-account-log rejects a chunked body over the
    # ceiling and appends no account-run line.
    record(
        "F2P-114",
        _body_too_large(hot["l8_chunked_over_append"])
        and hot["l8_chunked_over_log_before"] == hot["l8_chunked_over_log_after"],
        "status=%s server=%s error=%r log_delta=%d"
        % (hot["l8_chunked_over_append"]["status"],
           hot["l8_chunked_over_append"]["headers"].get("Server"),
           _payload_error(hot["l8_chunked_over_append"]),
           len(hot["l8_chunked_over_log_after"] or "")
           - len(hot["l8_chunked_over_log_before"] or "")),
    )
    # F2P-115: a chunked body of exactly MAX_BODY_BYTES is still accepted and
    # appends exactly one line (boundary negative control against over-rejection).
    record(
        "F2P-115",
        _append_has(
            hot["l8_chunked_exact_append"],
            hot["l8_chunked_exact_log_after"],
            L8_APPEND_EXACT_EXPECTED,
        )
        and _line_count(hot["l8_chunked_exact_log_after"])
        - _line_count(hot["l8_chunked_exact_log_before"]) == 1,
        "status=%s server=%s added=%d"
        % (hot["l8_chunked_exact_append"]["status"],
           hot["l8_chunked_exact_append"]["headers"].get("Server"),
           _line_count(hot["l8_chunked_exact_log_after"])
           - _line_count(hot["l8_chunked_exact_log_before"])),
    )
    # --- L8 Transfer-Encoding acceptance set: only a single chunked coding ----
    # F2P-116: custom /random-email rejects an unrecognized single coding and
    # inserts no random address.
    record(
        "F2P-116",
        _unsupported_te(cus["l8_te_gzip_random"])
        and cus["l8_te_gzip_db_before"] == cus["l8_te_gzip_db_after"],
        "status=%s server=%s error=%r rows=%s->%s"
        % (cus["l8_te_gzip_random"]["status"],
           cus["l8_te_gzip_random"]["headers"].get("Server"),
           _payload_error(cus["l8_te_gzip_random"]),
           cus["l8_te_gzip_db_before"], cus["l8_te_gzip_db_after"]),
    )
    # F2P-117: custom /random-email rejects a coding list (chunked, gzip).
    record(
        "F2P-117",
        _unsupported_te(cus["l8_te_chunked_gzip_random"]),
        "status=%s server=%s error=%r"
        % (cus["l8_te_chunked_gzip_random"]["status"],
           cus["l8_te_chunked_gzip_random"]["headers"].get("Server"),
           _payload_error(cus["l8_te_chunked_gzip_random"])),
    )
    # F2P-118: hotmail /append-account-log rejects a repeated Transfer-Encoding
    # header (chunked on one line, gzip on another) and appends no line.
    record(
        "F2P-118",
        _unsupported_te(hot["l8_two_te_append"])
        and hot["l8_two_te_log_before"] == hot["l8_two_te_log_after"],
        "status=%s server=%s error=%r log_delta=%d"
        % (hot["l8_two_te_append"]["status"],
           hot["l8_two_te_append"]["headers"].get("Server"),
           _payload_error(hot["l8_two_te_append"]),
           len(hot["l8_two_te_log_after"] or "")
           - len(hot["l8_two_te_log_before"] or "")),
    )
    # F2P-119: custom /health (which never consumes the body) rejects identity,
    # proving the coding check is envelope-level and precedes routing.
    record(
        "F2P-119",
        _unsupported_te(cus["l8_te_identity_health"]),
        "status=%s server=%s error=%r"
        % (cus["l8_te_identity_health"]["status"],
           cus["l8_te_identity_health"]["headers"].get("Server"),
           _payload_error(cus["l8_te_identity_health"])),
    )
    # F2P-120: a case-folded, space-trimmed `chunked` coding is accepted and
    # served normally (negative control against over-rejection).
    record(
        "F2P-120",
        _random_ok(cus["l8_te_case_random"]),
        "status=%s server=%s body=%r"
        % (cus["l8_te_case_random"]["status"],
           cus["l8_te_case_random"]["headers"].get("Server"),
           cus["l8_te_case_random"]["body"][:80]),
    )
    # F2P-121: an unknown path with an unsupported coding answers the coding
    # envelope, not the 404 envelope, proving the check precedes routing.
    record(
        "F2P-121",
        _unsupported_te(cus["l8_te_gzip_unknown"]),
        "status=%s server=%s error=%r"
        % (cus["l8_te_gzip_unknown"]["status"],
           cus["l8_te_gzip_unknown"]["headers"].get("Server"),
           _payload_error(cus["l8_te_gzip_unknown"])),
    )
    # --- L9 request-target byte fidelity -------------------------------------
    # F2P-122: custom echoes a raw, non-percent-encoded 0xC3 0xA9 target with the
    # source's latin-1 byte fidelity (``/cafÃ©``), not a re-encoded form.
    record(
        "F2P-122",
        _unknown_path_error(cus["l9_raw_target_upper"], RAW_TARGET_UPPER.decode("latin-1")),
        "status=%s server=%s error=%r"
        % (cus["l9_raw_target_upper"]["status"],
           cus["l9_raw_target_upper"]["headers"].get("Server"),
           _payload_error(cus["l9_raw_target_upper"])),
    )
    # F2P-123: hotmail echoes a raw 0xFF target as ``/cafÿ`` (latin-1), even
    # though that octet is not valid UTF-8.
    record(
        "F2P-123",
        _unknown_path_error(hot["l9_raw_target_high"], RAW_TARGET_HIGH.decode("latin-1")),
        "status=%s server=%s error=%r"
        % (hot["l9_raw_target_high"]["status"],
           hot["l9_raw_target_high"]["headers"].get("Server"),
           _payload_error(hot["l9_raw_target_high"])),
    )
    # F2P-124: the raw bytes keep their fidelity inside a query string too.
    record(
        "F2P-124",
        _unknown_path_error(cus["l9_raw_target_query"], RAW_TARGET_QUERY.decode("latin-1")),
        "status=%s server=%s error=%r"
        % (cus["l9_raw_target_query"]["status"],
           cus["l9_raw_target_query"]["headers"].get("Server"),
           _payload_error(cus["l9_raw_target_query"])),
    )
    # F2P-125: a percent-encoded target is echoed verbatim (negative control
    # against percent-decoding the echoed target).
    record(
        "F2P-125",
        _unknown_path_error(cus["l9_percent_target"], RAW_TARGET_PERCENT.decode("latin-1")),
        "status=%s server=%s error=%r"
        % (cus["l9_percent_target"]["status"],
           cus["l9_percent_target"]["headers"].get("Server"),
           _payload_error(cus["l9_percent_target"])),
    )
    # --- L9 zero-length entity is the empty object ---------------------------
    # F2P-126: custom /random-email with ``Content-Length: 0`` routes as {}, so
    # its own domain validation fails and no random address is inserted.
    record(
        "F2P-126",
        _framing_envelope(cus["l9_zero_length_random"], ZERO_LENGTH_RANDOM_ERROR)
        and cus["l9_zero_length_random_db_before"] == cus["l9_zero_length_random_db_after"],
        "status=%s server=%s error=%r rows=%s->%s"
        % (cus["l9_zero_length_random"]["status"],
           cus["l9_zero_length_random"]["headers"].get("Server"),
           _payload_error(cus["l9_zero_length_random"]),
           cus["l9_zero_length_random_db_before"], cus["l9_zero_length_random_db_after"]),
    )
    # F2P-127: hotmail /append-account-log with no Content-Length header at all
    # routes as {} and fails its own field validation, appending no line.
    record(
        "F2P-127",
        _framing_envelope(hot["l9_zero_length_append"], ZERO_LENGTH_APPEND_ERROR)
        and hot["l9_zero_length_log_before"] == hot["l9_zero_length_log_after"],
        "status=%s server=%s error=%r"
        % (hot["l9_zero_length_append"]["status"],
           hot["l9_zero_length_append"]["headers"].get("Server"),
           _payload_error(hot["l9_zero_length_append"])),
    )
    # F2P-128: ``Content-Length: 00`` is still a zero-length entity, so it routes
    # as {} rather than being treated as malformed or one byte, and inserts no
    # random address.
    record(
        "F2P-128",
        _framing_envelope(cus["l9_zero_padded_random"], ZERO_LENGTH_RANDOM_ERROR)
        and cus["l9_zero_padded_random_db_before"] == cus["l9_zero_padded_random_db_after"],
        "status=%s server=%s error=%r"
        % (cus["l9_zero_padded_random"]["status"],
           cus["l9_zero_padded_random"]["headers"].get("Server"),
           _payload_error(cus["l9_zero_padded_random"])),
    )
    # --- L9 empty / whitespace-only Content-Length ---------------------------
    # F2P-129: an empty Content-Length value is not a decimal integer, and it is
    # rejected before any random address is inserted.
    record(
        "F2P-129",
        _framing_envelope(cus["l9_empty_length_random"], INVALID_CONTENT_LENGTH_ERROR)
        and cus["l9_empty_length_random_db_before"] == cus["l9_empty_length_random_db_after"],
        "status=%s server=%s error=%r"
        % (cus["l9_empty_length_random"]["status"],
           cus["l9_empty_length_random"]["headers"].get("Server"),
           _payload_error(cus["l9_empty_length_random"])),
    )
    # F2P-130: a whitespace-only Content-Length value is not a decimal integer;
    # it is rejected before routing, so the account log gains no line.
    record(
        "F2P-130",
        _framing_envelope(hot["l9_ws_length_append"], INVALID_CONTENT_LENGTH_ERROR)
        and hot["l9_ws_length_log_before"] == hot["l9_ws_length_log_after"],
        "status=%s server=%s error=%r"
        % (hot["l9_ws_length_append"]["status"],
           hot["l9_ws_length_append"]["headers"].get("Server"),
           _payload_error(hot["l9_ws_length_append"])),
    )
    return results


def run(repo_root, out_path):
    work_parent = tempfile.mkdtemp(prefix="f2p-0004-")
    work_root = os.path.join(work_parent, "work")
    _copy_work_tree(repo_root, work_root)
    bundle = _collect(work_root)
    results = _evaluate(bundle)
    payload = {
        "batch_id": "BATCH-F2P-0004",
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
