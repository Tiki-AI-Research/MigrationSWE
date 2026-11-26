## Background
The `easy-sharing` command-line file and clipboard sharing service currently couples its HTTP routing, middleware pipeline, static serving, and multipart upload handling directly to Express 4 and Express-specific middleware packages (`express-fileupload`, `express-basic-auth`). This tight coupling limits modern asynchronous request handling and prevents direct integration with Fastify's lifecycle hooks and high-performance routing.

## Goal
- Migrate the HTTP server layer from Express 4 to Fastify 4 (`fastify@4.28.1` and `@fastify/multipart@8.3.1`).
- Completely remove Express runtime dependencies and Express-specific middleware without altering user-visible HTTP behaviors or CLI operation.
- Provide a standardized Fastify application factory `buildApp(options)` that returns an instance supporting lifecycle methods (`ready()`, `inject()`, and `close()`), while preserving the existing `start(options)` server startup entrypoint.
- Add `test/fastify-smoke.js` with a focused `buildApp()`/`inject()` lifecycle smoke test, and include it in the existing `npm test` script alongside `test/test.js`.

This is a framework migration from Express 4 to Fastify 4. It contains three required migration units: target dependency replacement, the `buildApp`/`start` application export contract, and removal of Express from production runtime code. Adding the focused Fastify lifecycle smoke test and preserving the existing regression suite are mandatory test-integrity constraints, not separate production migration units. The task does not involve persistent state (`persistent_state_involved=false`), so no state migration is required.

## Public contract to preserve
- `bin/index.js` must remain unchanged in its CLI interface, options, arguments parsing, and exit behaviors.
- `package.json` must retain `name: "easy-sharing"`, `main: "bin/index.js"`, and `bin: { "sharing": "./bin/index.js", "easy-sharing": "./bin/index.js" }`, and must set `engines.node: ">=14.18.0"`, the first Node 14 release that supports the `node:` imports used by Fastify 4.28.1.
- `bin/app.js` must export:
  - `start(options)`: Boots the HTTP/HTTPS server, returns an object containing the server instance, and preserves original startup semantics.
  - `buildApp(options)`: Factory function returning a real Fastify instance that exposes `.ready()`, `.inject()`, and `.close()`. Every option has a default, so `buildApp({})` constructs an instance without binding a port; the server starts listening only through `start()`.
- HTTP endpoints and routing semantics:
  - `GET /qr`: Render the QR-code fallback page.
  - `GET /receive`: Render the upload and text-receive form.
  - `POST /upload`: Receive multipart file uploads.
  - `POST /text`: Receive clipboard text.
  - `GET /share` and `GET /share/*`: Serve files and directory listings while preserving mount-prefix rewriting, URL encoding of listing links, and query-parameter handling.
  - `GET /zip`: Download a shared directory as a ZIP archive with correct headers and stream error handling.
  - `GET /clipboard`: Render the shared clipboard UI.
  - `GET /clipboard.txt`: Download the shared clipboard content as text.
- Authentication and security:
  - Basic authentication when configured via username/password options, returning 401 Unauthorized upon missing or invalid credentials.
  - Security token authentication when configured via `--token` / `token` option, returning 403 Forbidden or 401 Unauthorized as expected.

### Required Fastify smoke test (`test/fastify-smoke.js`)
- Construct the app via `buildApp(options)` and assert that the returned instance exposes `ready()`, `inject()`, and `close()` as functions.
- Exercise at least one route through `inject()` and assert its response status code.
- Call and await `close()` so resources are released.
- Keep it wired into the existing `npm test` script alongside `test/test.js`.

## Behavior that must stay unchanged
- Upload filename safety and collision resolution: Path traversal prevention (`../` stripping), reservation tracking, non-destructive renaming (`stem (1).ext`), symlink protection, and discarding control characters.
- Clipboard sharing: Text trimming, stripping dangerous terminal escape and control sequences, and updating system clipboard when enabled.
- Single-request mode (`--once`): Automatically shutting down the active server instance after fulfilling one valid download, receive, or clipboard operation.
- Error semantics and status codes: Preserve the original behavior for each route and error case, including existing responses for malformed requests.

## Allowed differences
- Allowed to differ: error-message wording / JSON error payload wording for the same error class (error paths are checked by HTTP status code); log output format or absence of logs; internal module layout, helper/function naming, and middleware/plugin implementation details; multipart boundary values and response header ordering.
- Still exact (not allowed to differ): per-route HTTP status codes; route paths and request/response semantics; served bytes (rendered HTML pages, file payloads, filenames); `content-disposition` semantics; the CLI surface and its behavior.
- The verifier compares responses between the original and migrated server and only tolerates differences outside those exact behaviors.

## Constraints
1. Must use CommonJS (`require` / `module.exports`) compatible with Node.js `>=14.18.0`. Do not use ES modules (`import` / `export`) in production sources or APIs unsupported on Node 14 (such as `stream/promises`).
2. Must NOT include `express`, `express-fileupload`, or `express-basic-auth` in production dependencies or require them anywhere in `bin/**`.
3. Must use exact production dependency versions `fastify@4.28.1` and `@fastify/multipart@8.3.1`. The committed lockfile must resolve a dependency tree whose declared Node engine ranges all accept Node 14.18.0; use a package-manager override when a newer transitive release drops Node 14 support.
4. Preserve the existing test suite in `test/test.js`: do not delete tests, remove assertions, change expected behavior, or add skipped tests (`.skip`, `.only`). The only permitted adaptation in that file is the frozen Windows filename-compatibility branch needed for platform-portable URL-significant filenames.
5. Allowed changes are limited to `bin/app.js`, `package.json`, `package-lock.json`, the exact platform-compatibility adaptation in `test/test.js`, and the new `test/fastify-smoke.js`. Do not modify `bin/index.js`, other production modules, evaluator-owned files, baseline markers, or reserved evaluation directories.
6. Do not introduce unrelated features, dependencies, generated files, or formatting rewrites. Do not invoke Express through string construction, aliases, dynamic properties, subprocesses, RPC, HTTP forwarding, or any other legacy-server bridge.
7. All runtime dependencies must be declared and locked. The migrated service and `npm test` must run without downloading packages or contacting an external service at runtime.

## Runtime
Candidate code, including `npm test`, is executed with **Node 14.18.0 / npm 9.2.0** in both the workspace and the evaluator. Dependency-installation tooling may use a newer Node, but it never executes candidate code.

## Validation

Run at least:

```text
npm ci --ignore-scripts --no-audit --no-fund
node --check bin/app.js
node --check bin/index.js
npm test
```

Compare the migrated server with the original implementation using representative valid and invalid requests before submitting.

The evaluator owns its reports under `/logs/verifier`, including `ctrf.json`, `reward-details.json`, and `reward.json`; candidate code must not create, modify, or depend on those reports.
