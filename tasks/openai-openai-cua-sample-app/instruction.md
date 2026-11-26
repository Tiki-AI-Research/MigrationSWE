# Migrate the JavaScript runner backend from Fastify to Express

## Background

`javascript-app/` implements the TypeScript/Playwright sample agent. Its HTTP backend is built on Fastify 5.12.1 and is shared with the operator console. The backend owns the API client, run lifecycle, event streams, artifact serving, and persisted output. The shared console calls this backend for all JSON and SSE traffic.

## Goal

- Migrate the JavaScript runner HTTP backend from Fastify 5.12.1 to Express 5.1.0.
- Express MUST provide the routing, request-body handling, CORS, error handling, and response streaming currently provided by Fastify.
- The migration scope is limited to `javascript-app/src/server.ts`, `javascript-app/src/index.ts`, `javascript-app/tests/server.test.ts`, and `javascript-app/package.json`.
- Out of scope: the `console/` Next.js operator console, the `labs/` browser templates, the `python-app/` FastAPI backend, and the `contracts/` shared schemas.

## Public contract to preserve

- `GET /health` MUST return `{"status":"ok","service":"runner"}` with HTTP 200.
- `GET /api/capabilities` MUST return `backendCapabilitiesSchema`, with `backendId: "javascript"`, `codeTool: "exec_js"`, `browserModes: ["headless","headful"]`, and `defaults` of `{browserMode:"headless", maxResponseTurns:24, model:<shared default model>}`. `instanceId` MUST come from `CUA_INSTANCE_ID` when set, otherwise a fresh id.
- `GET /api/scenarios` MUST return an array matching `scenariosResponseSchema`.
- `POST /api/scenarios/:id/reset` MUST return `scenarioResetResponseSchema`.
- `POST /api/runs` MUST return HTTP 202 and a `startRunResponseSchema` body.
- `GET /api/runs/active` MUST return `runDetailSchema` for the active run, or JSON `null` when idle.
- `GET /api/runs/:id` MUST return `runDetailSchema`.
- `POST /api/runs/:id/stop` MUST return `runDetailSchema`.
- `GET /api/runs/:id/replay` MUST return the version-3 replay bundle matching `replayBundleSchema`.
- `GET /api/runs/:id/artifacts/screenshots/:name` MUST return `image/png` bytes, or an `artifact_not_found` error.
- `GET /api/runs/:id/events` MUST return a `text/event-stream` response.
- All error responses MUST use the `{code, error, hint?}` shape of `runnerErrorResponseSchema`.
- `javascript-app/src/server.ts` MUST keep exporting `createServer(options?)` that accepts the existing `dataRoot`, `allowedOrigins`, and `manager` options and supports the start/stop usage in `src/index.ts` and the tests.
- `javascript-app/src/index.ts` MUST acquire the shared backend lease, listen on `HOST`/`PORT` (default `127.0.0.1:4001`), and print exactly `Runner listening on http://<host>:<port>` after a successful start.

## Behavior requirements

- If a request carries an `Origin` that is not allowed, respond 403 `origin_not_allowed` before performing any action; allowed JSON and SSE responses MUST set `Access-Control-Allow-Origin` to that origin and set `Vary: Origin`.
- On `OPTIONS`, respond 204 with `Access-Control-Allow-Headers: content-type, last-event-id, x-cua-backend` and `Access-Control-Allow-Methods: GET,POST,OPTIONS`.
- If `X-CUA-Backend` is present and is not `javascript`, respond 409 `backend_mismatch` before performing any action.
- If `POST /api/runs` receives unknown fields, a missing or empty `scenarioId`/`prompt`, or malformed JSON, respond 400 `invalid_request` and MUST NOT start a run.
- If `maxResponseTurns` exceeds 50, respond 400 `invalid_request` with an error message containing `maxResponseTurns`.
- If the removed `verificationEnabled` field is sent, respond 400 `invalid_request`.
- If a run id fails `^[A-Za-z0-9_-]+$` (including percent-encoded traversal), respond 400 `invalid_request`.
- If `GET /api/runs/:id` targets an unknown run, respond 404 `run_not_found` with a hint containing `Start a new run`.
- If a stored run replay has a version other than 3, respond 409 `unsupported_replay_version` for `GET /api/runs/:id`, `/replay`, `/events`, and `POST /stop`, and MUST leave the stored replay file unchanged.
- The screenshot route MUST return the PNG when it exists on disk and otherwise respond 404 `artifact_not_found`.
- SSE endpoints MUST send the persisted snapshot first, then live events, deduplicating by strictly increasing `sequence`, and MUST end the connection after `run_completed`, `run_failed`, or `run_cancelled`.
- Events emitted for a running run between the snapshot read and subscription MUST be delivered exactly once.
- If a streaming run has no live subscription, `GET /api/runs/:id/events` MUST respond with a JSON error envelope (`run_not_active`) rather than an SSE body.
- If an unexpected error occurs, respond 500 `internal_runner_error` without leaking the internal message, and log the original error.
- On graceful close, the server MUST wait for active executor teardown, invoke `manager.shutdown()` exactly once, and end live SSE streams.
- On `SIGINT`, `SIGTERM`, or `SIGHUP`, the process MUST shut down cleanly and exit with 130, 143, or 129 respectively; when `CUA_MANAGED_LAUNCH=1`, it MUST exit 0 once stdin ends.
- If coordination port 4050 is already held, the backend MUST fail to start with the single-backend constraint error.

## Constraints

1. The migration MUST NOT remove or rename public routes, HTTP status codes, response schemas, or error codes outside the stated goal.
2. Production code within scope MUST NOT import or call Fastify.
3. Production code within scope MUST NOT delegate the migrated responsibilities to the Fastify runtime through subprocesses, RPC, FFI, or dynamic loading.
4. Changes MUST be limited to the four in-scope files, plus the workspace lockfile and any directly required dependency metadata.
5. `javascript-app/package.json` MUST replace the Fastify dependency with Express 5.1.0; Fastify MUST NOT remain a runtime dependency of the runner package.
6. The shared `contracts/` schemas, the version-3 replay format, `console/`, `labs/`, and `python-app/` MUST NOT be modified.
7. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
8. `javascript-app/tests/server.test.ts` is in scope: adapt its harness to the Express server while preserving its behavioral assertions; a test-only adaptation MUST NOT weaken public behavior.
9. Work in `/workspace/repo`, starting from the provided baseline commit `f2a3dc523ae406f9b704f9a420a05402a63b4522`. Your changes are collected as a patch of the repository working tree relative to that commit, so keep all edits inside the repository.

## Validation

From `/workspace/repo`, run `pnpm check:js`, which runs lint, typecheck, tests, and build for the JavaScript workspace. The runner-specific checks are `pnpm --filter @cua-sample/runner test` and `pnpm --filter @cua-sample/runner typecheck`. These public checks exercise the preserved behavior; passing them does not by itself prove every requirement.
