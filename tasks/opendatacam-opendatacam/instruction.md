# Migrate the `server.js` HTTP server from Express to Fastify

## Background

`server.js` currently builds the OpenDataCam HTTP server with Express 4.17.1. It registers every API route, serves the generated API documentation, proxies the MJPEG stream, exposes the tracker SSE stream, handles video uploads, and forwards all remaining requests to the Next.js request handler.

## Goal

- Migrate the HTTP layer implemented in `server.js` from Express 4.17.1 to Fastify 4.28.1.
- Fastify MUST own routing, JSON body parsing, static file serving, Server-Sent Events, multipart upload handling, CSV export, and the final catch-all Next.js request handler that `server.js` currently implements.
- The Next.js request handler registered as the final catch-all route MUST keep the same request/response contract.
- The following are out of scope and MUST NOT change: Next.js pages, React components, Redux/Immutable state and any client-side rendering; YOLO/Darknet subprocess launch and monitoring and `node-moving-things-tracker` behavior; the MongoDB persistence contracts for the recordings/tracker/counter/app collections; the `config.json` schema, ports and environment handling; and the enabled GPS handling.

## Public contract to preserve

- The route table and methods MUST stay the same: `GET /`, `GET /start`, `GET /webcam/stream`, `GET /webcam/resolution`, `GET /console`, `POST /counter/areas`, `GET /counter/areas`, `GET /tracker/sse`, `GET /recording/start`, `GET /recording/stop`, `GET /recordings`, `GET /recording/:id/tracker`, `GET /recording/:id`, `DELETE /recording/:id`, `GET /recording/:id/counter`, `GET /recording/:id/counter/csv`, `GET /status`, `GET /config`, `GET /files`, `POST /files`, `GET /ui`, `POST /ui`, `GET /api/doc`, and the catch-all route.
- Success and error status codes MUST stay the same, including `200` for the start/stop actions and `POST /counter/areas`, `404` for `GET /recording/:id/counter` when no counter data exists, and `500` on the corresponding failure paths.
- `GET /config` and `GET /status` MUST return the same fields as the current Express implementation, adding or removing none.
- `GET /tracker/sse` MUST send JSON Server-Sent Events containing `videoResolution`, `trackerDataForLastFrame`, `appState`, `counterSummary`, and `trackerSummary`.
- `POST /files` MUST accept one multipart file in the `video` field, store it through disk storage into the configured video upload folder, and write the stored file path to the response as the current implementation does.
- `GET /recording/:id/counter/csv` MUST return a CSV download whose header row is exactly the one the current implementation emits — `timestamp,area,name,id,bearing,countingDirection` — with a download content type.
- `GET /api/doc` MUST serve the static `.build/apidoc` directory, and the catch-all route MUST forward all remaining requests to the Next.js handler.

## Behavior requirements

- Every specific route MUST be matched before the catch-all Next.js handler, preserving the original registration order.
- Global JSON body parsing MUST keep `bodyParser.json()` semantics, including the default body size limit and the malformed-body error response.
- `GET /webcam/stream` MUST proxy the MJPEG stream from `config.PORTS.darknet_mjpeg_stream` without buffering or rewriting frames.
- `GET /webcam/resolution` MUST return the `{w,h}` resolution captured from Darknet stdout parsing at the same lifecycle point as the current implementation.
- `GET /console` MUST first write the captured stdout buffer and then push newly captured stdout on the same interval, closing any previous console stream.
- Process signal handling MUST still call `Opendatacam.clean()` on exit, SIGINT, SIGUSR1 and SIGUSR2, and MUST keep the 30000-character stdout ring buffer.
- At startup, when the `package.json` version differs from `config.OPENDATACAM_VERSION`, the process MUST exit with the same error behavior as the current implementation.
- The server MUST listen on `process.env.PORT` when set, otherwise on the application port resolved from `config.json`, preserving the existing default.
- `GET /`, `GET /start`, `GET /recording/start`, `GET /recording/stop`, `POST /counter/areas`, and `POST /ui` MUST keep the same side effects on `Opendatacam`, the YOLO process, and the tracker that the current handlers trigger.

## Constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production code in the migration scope MUST NOT import or call Express or its middleware (`serve-static`, `body-parser`, `csv-express`, `server-sent-events`) for the migrated responsibilities.
3. Production code in the migration scope MUST NOT delegate the migrated responsibilities to an Express runtime through a subprocess, RPC, FFI, or dynamic loading.
4. Changes MUST be limited to `server.js` and its dependency configuration. You may add or remove npm dependencies and Fastify plugins, but the code you leave behind MUST run against the dependencies the evaluation environment provisions. The environment provides the target stack (Fastify and the Fastify plugins/utilities shipped with it) together with their transitive dependencies; it does **not** provide the Express stack (`express`, `serve-static`, `body-parser`, `csv-express`, `server-sent-events`, `multer`) or any of their transitive dependencies, and it cannot install additional packages at evaluation time. Any package your `server.js` resolves at runtime must therefore come from the provided set; a program that requires a package the environment cannot resolve will fail to start, and that failure is scored as a failed migration, not as an environment error.
5. Express and its middleware are used only by `server.js`; you MAY remove them from dependency configuration if they become unused.
6. Modules outside the migration scope, and the `config.json`, port, environment and MongoDB contracts, MUST NOT be changed.
7. The existing public specs under `spec/` are protected; you MUST NOT delete or weaken their assertions.
8. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
9. Work in `/workspace/repo`. Keep all migration changes in the working tree; the evaluator collects the repository diff against the task's starting commit.

## Validation

From `/workspace/repo`, run `npm test` (which runs the Jasmine suite through `scripts/npm/test.sh`).

This exercises the unchanged server modules (`Opendatacam`, `MongoDbManager`, `Recording`, `YoloDarknet`, `configHelper`, and the YOLO simulation); it does not by itself verify the migrated HTTP surface. A passing run is not proof that every requirement above is met.
