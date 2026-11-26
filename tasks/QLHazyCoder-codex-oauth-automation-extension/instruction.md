# Migrate the local mail helper scripts from Python http.server to Flask

## Background

`scripts/hotmail_helper.py` and `scripts/custom_mail_helper.py` expose local HTTP JSON APIs that the FlowPilot browser extension calls.

Both modules currently implement their servers with Python 3.10's standard-library `http.server` (`BaseHTTPRequestHandler` and `ThreadingHTTPServer`).

The browser extension is a Chrome MV3 runtime that loads as frontend assets. It is out of scope for this task.

## Goal

- Migrate the HTTP serving layer of `scripts/hotmail_helper.py` and `scripts/custom_mail_helper.py` from Python 3.10's standard-library `http.server` to Flask 3.1.2.
- Flask MUST provide the HTTP routing, JSON request parsing, response writing, CORS header handling, and request-envelope validation (request framing and a fixed request body-size limit) for both helpers.
- Keep both helpers runnable as their existing entry points, with the same bind host and port behavior.

## Public contract to preserve

- `scripts/hotmail_helper.py` MUST keep serving on `127.0.0.1:17373` and accept `POST` requests with a JSON body.
- `scripts/custom_mail_helper.py` MUST keep serving on `127.0.0.1` and honor `FLOWPILOT_CUSTOM_MAIL_HELPER_PORT`, which defaults to `17374`.
- `hotmail_helper` MUST keep the routes `POST /messages`, `POST /code`, `POST /append-account-log`, and `POST /sync-account-run-records`.
- `custom_mail_helper` MUST keep the routes `POST /messages`, `POST /code`, `POST /health`, `POST /env`, and `POST /random-email`.
- `OPTIONS` preflight requests MUST return HTTP 204 with the CORS headers and the JSON response content type (`application/json; charset=utf-8`), including for an unknown path, and with an empty body.
- Every response MUST be UTF-8 JSON with `Access-Control-Allow-Origin: *`, `Access-Control-Allow-Headers: Content-Type`, and `Access-Control-Allow-Methods: POST, OPTIONS`.
- Successful responses MUST stay JSON objects with `"ok": true`. The existing field names MUST be kept, including `messages`, `code`, `usedTimeFallback`, `mailboxResults`, `nextRefreshToken`, `tokenEndpoint`, `transport`, and `filePath`.
- Failures MUST return HTTP 500 with `{"ok": false, "error": <message>}`.
- Unknown paths MUST return HTTP 404 with `{"ok": false, "error": "Unsupported path: <path>"}`. `<path>` here is the original request target exactly as it arrived on the wire — including any query string, percent-encoding, and trailing slash — not a decoded, normalized, or re-serialized form. The serialization of failure and unknown-path bodies is itself part of the contract: it MUST match the source byte for byte, including `, ` between entries, `: ` between each key and its value, and no trailing newline. In particular the raw request-target octets MUST be preserved by decoding them as ISO-8859-1 (latin-1), exactly as the source's request-line decoding does: a non-percent-encoded byte such as `0xC3 0xA9` MUST appear in the echoed `error` string as the two characters `Ã©`, and a non-UTF-8 byte such as `0xFF` as `ÿ`. A framework's own request-target values MUST NOT be used for this echo when they percent-decode or re-encode the target.
- Route matching MUST decide which documented route serves a request from the **decoded URL path**, not from the raw request target. A served route MUST still be served when the request target carries a query string (for example `POST /health?x=1` or `POST /append-account-log?x=1`) or when a path byte is percent-encoded (for example `POST /he%61lth`, which decodes to `/health`); the query string MUST NOT take part in route matching. This is the counterpart of the unknown-path rule above: matching uses the decoded path, while the 404 `error` still echoes the original request target verbatim.
- A request whose target is in **absolute form** (for example `POST http://127.0.0.1:17373/messages HTTP/1.1`) MUST be routed by its path component, exactly like the equivalent origin-form request (`POST /messages`); its query string, if present, MUST be stripped before matching as above.
- For the documented routes that consume a JSON request body — `hotmail_helper` `POST /messages`, `POST /code`, `POST /append-account-log`, and `POST /sync-account-run-records`, and `custom_mail_helper` `POST /messages`, `POST /code`, and `POST /random-email` — a request body that is not valid JSON MUST fail with HTTP 500 and `{"ok": false, "error": "Invalid JSON payload: <detail>"}`, where `<detail>` is exactly the message the source produced by decoding the raw body with the standard library `json.loads` (for example `Expecting property name enclosed in double quotes: line 1 column 2 (char 1)`). Framework-specific parse error text (such as Flask's `400 Bad Request`) MUST NOT replace it.
- Routing MUST be resolved before any request body is parsed: Flask owns dispatch, and each view parses only the body it needs. Consequently the malformed-body rule above applies only to a route that actually consumes the body:
  - a request that matches no documented route — an unknown path, or an unsupported method on a documented path — MUST receive the unknown-path envelope (HTTP 404 and `{"ok": false, "error": "Unsupported path: <path>"}`) even when its body is malformed, empty, or not decodable as UTF-8;
  - the documented routes that do not consume a JSON body — `custom_mail_helper` `POST /health` and `POST /env` — MUST be served successfully (`{"ok": true}` and `{"ok": true, "env": {…}}` respectively) regardless of the request body, including a malformed, blank, or non-UTF-8 one.
  The source runtime's habit of parsing the body before it dispatches MUST NOT be carried over to the migrated helpers.
- A matched route that consumes a JSON body and receives a **zero-length entity** MUST treat the payload as the empty JSON object `{}` and then run its own field validation; it MUST NOT report `Invalid JSON payload: …` for a zero-length body. A body is zero-length when no request body bytes are present, whether because no `Content-Length` header was sent or because the declared length is zero (including a zero written with leading digits, such as `00`). Consequently `custom_mail_helper` `POST /random-email` with a zero-length body MUST fail its own missing-domain validation and `hotmail_helper` `POST /append-account-log` with a zero-length body MUST fail its own missing-field validation.
- A request body framed with `Transfer-Encoding: chunked` MUST be parsed exactly like the equivalent `Content-Length` body: the de-chunked bytes are the JSON payload. A valid chunked body MUST be served exactly like its `Content-Length` twin (same status and same `"ok": true` payload, for example the same random addresses or the same appended log line or account-run snapshot), and a malformed chunked body MUST fail with the same HTTP 500 `{"ok": false, "error": "Invalid JSON payload: <detail>"}` envelope described above. The framing MUST NOT cause the payload to be ignored, and the source runtime's behavior of reading only `Content-Length` bytes MUST NOT be carried over.
- Both helpers MUST validate the request envelope before a matched route reads the body:
  - A request that carries both a `Content-Length` header and a `Transfer-Encoding` header has ambiguous framing and MUST fail with HTTP 500 and `{"ok": false, "error": "Ambiguous request framing"}`; the helper MUST NOT pick one framing and process the body.
  - A request whose `Content-Length` header is present but is not a non-negative decimal integer MUST fail with HTTP 500 and `{"ok": false, "error": "Invalid Content-Length"}`. After trimming optional surrounding whitespace, the value MUST consist only of the decimal digits `0`-`9`; an empty value and a value that is only whitespace therefore fail this check.
  - A request whose declared `Content-Length` is greater than `MAX_BODY_BYTES`, fixed at `1048576` bytes (1 MiB), MUST fail with HTTP 413 and `{"ok": false, "error": "Request body too large"}`. An oversized request MUST be rejected before routing and before the body is read, and MUST NOT reach any route handler or produce any side effect (no appended account-run line, no account-run snapshot write, no inserted random address).
  - The `MAX_BODY_BYTES` ceiling applies to the **decoded** request entity body, not only to a declared `Content-Length`: a request framed with `Transfer-Encoding: chunked` whose chunks decode to more than `1048576` bytes MUST fail with HTTP 413 and `{"ok": false, "error": "Request body too large"}` before the matched body-consuming route performs any work or produces any side effect (no inserted random address, no appended account-run line, no account-run snapshot write). A decoded body of exactly `1048576` bytes MUST still be accepted and served normally.
  - The only accepted `Transfer-Encoding` is a single `chunked` coding. After case-folding and trimming optional surrounding whitespace, the accepted value is exactly `chunked` — so `chunked`, `Chunked`, and `  chunked  ` are accepted. Any other present value MUST fail with HTTP 500 and `{"ok": false, "error": "Unsupported Transfer-Encoding"}` before routing and before any body is read, and MUST NOT produce any side effect. This covers an unrecognized single coding (`gzip`, `identity`), a coding list or combination (`chunked, gzip`, `chunked,chunked`), a parameterized coding (`chunked;q=1`), an empty value, and repeated `Transfer-Encoding` header lines (which are equivalent to the comma-joined combination of those values). When both a `Content-Length` and any `Transfer-Encoding` are present, the request is still `Ambiguous request framing`; that check takes precedence.
  - The framing and encoding checks above (`Ambiguous request framing`, `Invalid Content-Length`, `Unsupported Transfer-Encoding`) are evaluated before route resolution, so they are returned with their own status and `error` text even when the request target is unknown, the method is unsupported, or the matched route does not consume the body. The framing checks take precedence over the body-size check. The declared-`Content-Length` ceiling is also evaluated before routing; the decoded-body ceiling is evaluated before the matched body-consuming route performs its work. All of them are returned even when the body would otherwise be treated as malformed.
- Requests that use an unsupported HTTP method (for example a non-`POST` method on a served path) MUST be answered with the same JSON error envelope as unknown paths — HTTP 404 and `{"ok": false, "error": "Unsupported path: <path>"}` — and MUST NOT fall through to the server's default HTML error page.
- Both modules MUST stay importable, so existing tests can load them and call `select_latest_code`, `refresh_access_token`, `fetch_graph_messages`, and `get_json` directly.

## Behavior that must stay unchanged

- If a `hotmail_helper` `POST /messages` or `POST /code` request omits `email`, `clientId`, or `refreshToken`, the helper MUST return the failure envelope with HTTP 500.
- `custom_mail_helper` random-email generation MUST insert every produced address into the SQLite `generated_emails` table, whose `email` column stays `UNIQUE`.
- If random-email generation cannot produce `n` unique addresses, it MUST roll back the whole batch and return the failure envelope.
- `hotmail_helper` `POST /sync-account-run-records` MUST keep normalizing records and MUST write the snapshot JSON with its summary counts.
- Normalization MUST keep `finalStatus` constrained to `success`, `failed`, or `stopped`, and keep the `retryCount`, `failedStep`, and `autoRunContext` fields.
- `hotmail_helper` `POST /append-account-log` MUST append exactly one tab-separated line with `recordedAt`, `email`, `password`, `status`, and `reason`, under the shared lock.
- `custom_mail_helper` `/random-email` MUST keep returning `email`, `emails`, `domain`, and `count` next to `"ok": true`.
- `custom_mail_helper` MUST keep loading its `.env` file from the project root for its configuration.
- Concurrent `POST` requests to either helper MUST be served without interleaving response bodies.

## constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production code in `scripts/hotmail_helper.py` and `scripts/custom_mail_helper.py` MUST NOT import or call `http.server` for the migrated HTTP responsibilities.
3. The migrated helpers MUST NOT delegate the HTTP serving to the source runtime, for example through a subprocess, RPC, FFI, or dynamic loading of an `http.server`-based server.
4. Changes MUST be limited to `scripts/hotmail_helper.py`, `scripts/custom_mail_helper.py`, and the dependency configuration needed to declare Flask.
5. IMAP, Graph, and Outlook message normalization, verification-code extraction order, token-refresh endpoint ordering, and the `success`/`failed`/`stopped` status vocabulary MUST remain unchanged.
6. Response envelope keys and the account-run-history file and SQLite formats consumed by the extension MUST NOT be renamed or reshaped.
7. Browser-extension assets (`manifest.json`, `rules.json`, `background/*`, `content/*`, `flows/*`, `sidepanel/*`) MUST NOT be modified or re-implemented.
8. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode inputs to fake compliance.
9. Work in `/workspace/repo`. Keep all required changes inside that repository working tree, because the submission is collected as a repository diff against the starting checkout.
10. From `/workspace/repo`, run `node --test tests/*.test.js`. This public command runs the extension test suite and checks that the JavaScript runtime still behaves as before. It does not by itself cover the migrated Python helpers.

IMPORTANT: Create a new branch from the task's base commit bf6049f196278eba0f1563f190c3934453fa548f and commit all task changes, including new files, when you are done.
