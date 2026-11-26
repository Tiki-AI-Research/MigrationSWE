# Migrate the AlphaClaw backend HTTP framework from Express to Hono

## Background

AlphaClaw's backend creates an Express 5.2.1 application. That application serves the Setup UI, authenticated setup APIs, gateway proxy, webhook intake, server-sent events, and WebSocket upgrade routing. The Setup UI, CLI, OpenClaw gateway lifecycle, and persistence layers are outside this migration.

## Goal

- Migrate the backend HTTP layer from Express 5.2.1 (declared as `^5.2.1`) to Hono 4.13.7.
- Hono MUST provide HTTP server integration, route matching, middleware composition, request parsing and limits, static file serving, response handling, SSE streaming, and proxy dispatch for this server.
- The migrated server MUST run on Hono directly. It MUST NOT keep the Express application as the serving runtime.
- Preserve every public route, authentication, body-handling, proxy, streaming, and lifecycle contract in this document.
- Keep the Setup UI, CLI, OpenClaw gateway dependency, gateway protocol, gateway subprocess orchestration, and database schemas out of scope.

## Public contract to preserve

- The complete route surface registered by the backend setup MUST keep its methods, paths, authentication requirements, status codes, and JSON field structure. This includes `/api/*`, `/auth/*`, `/health`, `/setup`, `/`, `/openclaw*`, `/assets/*`, `/hooks/*`, `/webhook/*`, `/oauth/:id`, `/v1/*`, and `/gmail-pubsub`.
- `POST /api/auth/login` MUST accept a JSON body with `password`. On success it MUST return `200 {"ok":true}` and set `setup_token` with `HttpOnly`, `SameSite=Lax`, `Path=/`, and a 7-day max age.
- Invalid credentials MUST return `401 {"ok":false,"error":"Invalid credentials"}`. A throttled client MUST receive `429` with `Retry-After` and `{"ok":false,"error":"Too many attempts. Try again shortly.","retryAfterSec":<seconds>}`.
- A missing `SETUP_PASSWORD` MUST produce the existing `503` error. Login, status, and logout MUST remain reachable without an authenticated session.
- `GET /api/auth/status` MUST return `{"authEnabled":<boolean>}`. `POST /api/auth/logout` MUST clear `setup_token` and return `{"ok":true}`.
- Setup authentication MUST use only the `setup_token` cookie. It MUST NOT accept a token from the query string.
- `/auth/google/callback`, `/auth/codex/callback`, and `/oauth/:id` MUST remain reachable without a setup session. Unauthenticated protected `/api/*` requests MUST receive `401 {"error":"Unauthorized"}`. Unauthenticated requests to protected non-API routes MUST receive a `302` redirect to `/login.html`. Public paths, including `GET /health`, `/login.html`, and static files served from the existing public asset root, MUST NOT be redirected.
- `/webhook/*`, `/hooks/*`, and `/gmail-pubsub` MUST receive raw request bytes, not a parsed JSON object, with the existing 5 MB limit. The bytes forwarded to the gateway MUST remain suitable for the existing signature validation.
- Other JSON endpoints MUST use the existing 5 MB JSON limit. Enabled `/v1/*` OpenAI-compatible requests MUST use the existing 50 MB JSON limit. Disabled `/v1/*` requests MUST return `404 {"error":"Not found"}` before parsing.
- `/openclaw` and `/openclaw/*` MUST require setup auth, strip the `/openclaw` prefix, and proxy to the gateway. A `/api/*` request that does not match a registered setup API prefix MUST proxy to the gateway. `/v1/*` OpenAI-compatible paths MUST proxy only when the feature is enabled and the bearer token matches the configured gateway token.
- Gateway proxy paths other than `/v1` MUST preserve the gateway status, headers, and body. The `/v1` proxy MUST strip hop-by-hop headers and `Set-Cookie` before streaming the gateway response. Gateway connection failures MUST return `502 {"error":"Gateway unavailable"}`.
- `GET /health` MUST remain public and return `{"status":<status>,"gateway":<gateway>}`, where `status` is `healthy` or `starting` and `gateway` is `running` or `starting`.
- `GET /` and `GET /setup` MUST continue to return the existing setup page under the existing setup-auth gating.
- Static files under the existing public asset root MUST remain available at their current URLs. `/login.html` MUST remain reachable without setup auth. Requests under `/assets/*` that are not served by the static root MUST retain the authenticated gateway proxy behavior.
- `GET /api/events/status` MUST remain a `text/event-stream` response. It MUST send an initial `status` event, repeat status events about every 2 seconds, send `: keepalive` comments about every 15 seconds, and stop its timers when the client disconnects.
- `GET /api/operations/:operationId/events` MUST remain a `text/event-stream` response with the existing `: connected` preamble, replayed events, live events, and disconnect cleanup.
- WebSocket upgrades for `/openclaw*`, `/api/watchdog/terminal/ws`, and `/api/ws/chat` MUST keep the existing cookie authentication. Unauthenticated upgrades MUST receive `401` before any gateway or terminal bridge. Authenticated terminal and chat sockets MUST remain bidirectionally usable. Other upgrades MUST continue to proxy to the gateway.
- The server MUST listen on the configured `PORT` (default `3000`) on `0.0.0.0`, emit a startup log, and run the onboarding boot sequence only when the instance is already onboarded.
- `SIGTERM` and `SIGINT` MUST stop the Gmail watch service and dispose the watchdog terminal session as they do today.

## Behavior that must stay unchanged

- A successful login MUST clear the client's failure count. Repeated failed logins MUST trigger the existing per-client and global throttles, including when proxy-derived client keys rotate.
- The configured proxy hop count (`TRUST_PROXY_HOPS`, default `1`) MUST continue to determine client IP addresses used by login and OpenAI-compatible throttling.
- Requests that exceed an applicable body limit MUST be rejected before any gateway forwarding or handler side effect.
- For `/webhook/*`, the path MUST be rewritten to `/hooks/*` for the gateway. A GET hook MUST be forwarded as POST.
- A `token` query parameter MUST be removed from the forwarded webhook URL. It MUST become a bearer token only when no `Authorization` header is present.
- Webhook query parameters MUST be merged into the forwarded JSON body. Explicit body values MUST take precedence.
- Gmail duplicate suppression MUST return `200 {"ok":true,"deduped":true}` without forwarding the request.
- Webhook gateway responses MUST mirror the gateway status, headers, and body. A gateway failure MUST record the request with status `502` and return `502 {"error":"Gateway unavailable"}`.
- `/oauth/:id` MUST look up the callback, mark it used, rewrite the request to `/hooks/<hookName>`, and forward it through the webhook middleware. When `WEBHOOK_TOKEN` is set, it MUST add `Authorization: Bearer <WEBHOOK_TOKEN>` to the forwarded request.
- The `/v1` proxy MUST strip `Host`, `Connection`, `Content-Length`, `Transfer-Encoding`, `Content-Encoding`, and `Cookie` from the upstream request. It MUST set `Content-Length` from the forwarded body, default `Content-Type` to `application/json` when a body is present, and preserve the original encoded path and query string.
- `/v1` authentication failures MUST return `401 {"error":"Unauthorized"}`. Repeated failures MUST return `429` with `Retry-After` and the existing `retryAfterSec` JSON field. Successful authentication MUST clear the failure count.
- `/v1` gateway responses MUST strip hop-by-hop headers and `Set-Cookie`, then stream the gateway status and body to the client.
- SSE responses MUST keep `Content-Type: text/event-stream`, `Cache-Control: no-cache, no-transform`, and `X-Accel-Buffering: no`. The status stream payload MUST retain the existing `status`, `watchdogStatus`, `doctorStatus`, and `timestamp` fields.
- `GET /api/watchdog/logs` MUST retain its `text/plain; charset=utf-8` response.
- WebSocket authentication failures MUST write a `401` response and close the connection before any terminal, chat, or gateway bridge starts.

## constraints

1. The migration MUST NOT remove public capabilities or change their contracts outside the stated goal.
2. Production code in the migrated HTTP layer MUST NOT require, import, call, or dynamically load Express for routing, middleware, body parsing, static serving, SSE, or proxying.
3. The migrated layer MUST NOT delegate those responsibilities to Express through a subprocess, RPC, FFI, or another runtime bridge.
4. Remove the source HTTP framework from AlphaClaw's direct dependencies. Declare the target framework at the version stated in the Goal as a direct runtime dependency, plus only the adapter and WebSocket helper packages required to preserve the existing Node behavior.
5. Express MAY remain installed only as a transitive dependency of `openclaw`. `http-proxy` and `ws` MAY remain for gateway proxying and WebSocket transport. They MUST NOT be used to keep Express as the HTTP framework.
6. Do not change HTTP route paths, methods, JSON field structure, or status-code semantics.
7. Do not modify the Preact/htm Setup UI, CSS, browser routing, components, or generated frontend assets.
8. Do not upgrade or replace `openclaw`, change the gateway protocol, or change gateway subprocess orchestration.
9. Do not modify SQLite schemas, primary keys, or aggregation results for usage, watchdog, doctor, webhooks, or auth.
10. Do not change the CLI commands, arguments, or exit codes.
11. Update the package manifests and lockfile consistently. Treat them as the dependency source of truth. The checked-in alternate lockfile importer is stale. Do not change the declared Node engine constraint.
12. The existing public tests, fixtures, and route contracts are protected. You MAY adapt public test harness wiring that directly depends on the source framework, including replacing the application construction with the target runtime's test setup. You MUST NOT delete tests, skip tests, weaken assertions, loosen comparisons, or hardcode expected inputs or outputs. Update the public runtime-guard test so it validates the migrated runtime instead of requiring the source framework.

The complete list of public test files permitted for harness adaptation is:

- `tests/server/express-runtime-guard.test.js`
- `tests/server/oauth-callback-middleware.test.js`
- `tests/server/routes-agents.test.js`
- `tests/server/routes-auth.test.js`
- `tests/server/routes-browse.test.js`
- `tests/server/routes-codex.test.js`
- `tests/server/routes-cron.test.js`
- `tests/server/routes-doctor.test.js`
- `tests/server/routes-models.test.js`
- `tests/server/routes-nodes.test.js`
- `tests/server/routes-onboarding.test.js`
- `tests/server/routes-pairings.test.js`
- `tests/server/routes-proxy.test.js`
- `tests/server/routes-system.test.js`
- `tests/server/routes-usage.test.js`
- `tests/server/routes-watchdog-test-notification.test.js`
- `tests/server/routes-watchdog.test.js`
- `tests/server/routes-webhooks.test.js`
- `tests/server/webhook-middleware.test.js`
- `tests/server/gmail-push.test.js`
- `tests/server/operation-events.test.js`

For these files, only application construction, fixture setup, and test-client wiring may change. Keep the same HTTP-level assertions: status codes, JSON fields and values, queue/config effects, and error details. All other public tests are protected and must not be modified, deleted, skipped, or weakened.

Do not move or delete existing test cases. Adapt them in place; add new tests separately. Adding a new test does not replace or satisfy a removed existing test.

13. Work from `/workspace/repo`. Keep all required source, dependency, test, and configuration changes in the repository working tree so they can be collected as a patch against the provided starting point. Do not replace the starting point with `main` or move required changes outside the repository.
14. From `/workspace/repo`, install dependencies if they are not already present, then run `npm test`. This public check covers part of the contract above and does not by itself prove every requirement.

IMPORTANT: Create a new branch from the task's base commit dfe3b2f09b0200a21481e51e9a9b240376d09061 and commit all task changes, including new files, when you are done.
