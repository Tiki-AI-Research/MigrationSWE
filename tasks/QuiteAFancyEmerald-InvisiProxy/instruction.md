# Migrate the InvisiProxy HTTP backend from Fastify to Express

## Background

InvisiProxy serves its web application from `src/server.mjs`. This module currently builds its HTTP layer on Fastify 5.12.3 with `@fastify/static` and `@fastify/helmet`, and runs a mrrowisp Wisp WebSocket server on a shared `node:http` server. The same module derives page routes from `src/routes.mjs`, disguises page paths when `disguiseFiles` is enabled, and serves build output from `views/dist`.

## Goal

- Migrate the HTTP serving layer in `src/server.mjs` from Fastify 5.12.3 to Express 5.1.0.
- Express MUST provide the responsibilities currently supplied by Fastify, `@fastify/static`, and `@fastify/helmet`: request routing, static file serving, response-header middleware, and duplicate/trailing slash route normalization.
- The mrrowisp Wisp server and its WebSocket upgrade wiring MUST stay on the same HTTP server.
- Out of scope and MUST remain unchanged: the client service worker and proxy transport assets (`views/sw.js`, `views/sw-blacklist.js`, `views/uv/`, `views/scram/`); build-time template injection and source rewriting (`src/templates.mjs`, `src/source-rewrites.mjs`, and the `build` flow in `run-command.mjs`); the mrrowisp connection, reputation, and WebSocket protocol configuration; the keys, ports, and environment-variable semantics of `config.json` and `ecosystem.config.js`; and the `views/dist` artifact layout and local blocklist behavior.

## Public contract to preserve

- The server MUST listen on the host from `config.json.host` and the port from `ecosystem.config.js` (`env`/`env_production.PORT`), as composed into `serverUrl` by `src/routes.mjs`.
- `GET <pathname>:path` MUST return the file under `views/dist` selected by the `pages` map in `src/routes.mjs`, using the content type resolved from the file extension with the configured default fallback.
- When `disguiseFiles` is enabled, non-`.ico` page paths MUST return the loader page `views/dist/pages/misc/deobf/loader.html`; the error page, a `.ico`-displaced page, and (when `serverUrl.pathname` is not `/`) `pages.index` served at `<pathname>` MUST use content type `image/vnd.microsoft.icon`, whereas an exempt default-page request keeps the file-extension content type resolved in the bullet above.
- Unmatched paths MUST return the pre-rendered 404 error page derived from `views/error.html`, using the disguised content type.
- `GET <pathname>github/:redirect` MUST redirect to the target for a known key in `externalPages.github` and MUST return 404 for an unknown key.
- Static file service MUST be preserved for the prefixes `assets`, `uv`, `scram`, `epoxy`, `libcurl`, and `baremux`, served from `views/dist` at their prefixed paths.
- `GET <pathname>sw.js` and `GET <pathname>sw-blacklist.js` MUST respond with content type `application/javascript` and the header `Service-Worker-Allowed: <serverUrl.pathname>`.
- WebSocket upgrade requests to `<pathname>wisp/` MUST be handed to the mrrowisp server.
- `GET <pathname>test-shutdown` MUST gracefully stop the Wisp server and the HTTP server and delete `src/.shutdown` when that file exists.
- Responses MUST continue to carry the security headers currently applied by the Helmet middleware with content security policy disabled, and the server MUST NOT emit an `X-Powered-By` header.

## Behavior that must stay unchanged

- The router MUST match duplicate-slash and trailing-slash request paths as if they were normalized.
- A `GET` for `<pathname>favicon.ico` MUST return an empty response body.
- When `disguiseFiles` is enabled, a request for a page path that is not exempt and does not end in `.ico` MUST return the loader page with content type `text/html` instead of the real page.
- The disguise exemptions MUST be preserved: paths ending in a file extension other than `.html` or `.ico`, paths under the static prefixes (`assets`, `uv`, `scram`, `epoxy`, `libcurl`, `baremux`, `wisp`), the exempt page keys (`login`, `test-shutdown`, `favicon.ico`, and each string-valued `externalPages` key), and -- when `pages.default === 'login'` -- the root path (empty path, i.e. `GET <pathname>`) MUST bypass the disguise.
- When `disguiseFiles` is enabled, a `.ico`-suffixed path that is not in `pages` and is not `favicon.ico` MUST have its `.ico` suffix stripped and be served as the underlying page, with content type `image/vnd.microsoft.icon` and header `Access-Control-Allow-Origin: null`.
- When a document request (`sec-fetch-dest: document`) carries the cookie `HistoryHide=true`, the response status MUST be changed to `404`.
- A disguised request for an unknown key of `<pathname>github/:redirect` MUST return the disguised 404 page.
- When `serverUrl.pathname` is not `/`, the server MUST NOT install a catch-all 404 handler and MUST serve `pages.index` at `<pathname>`.

## constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production code within the migration scope MUST NOT import or call Fastify, `@fastify/static`, or `@fastify/helmet` for the migrated responsibilities.
3. Production code within the migration scope MUST NOT delegate the migrated responsibilities to the Fastify runtime through child processes, RPC, FFI, or dynamic loading.
4. Changes MUST be limited to `src/server.mjs`, the dependency manifests (`package.json`, `pnpm-lock.yaml`), and supporting dependency configuration. `src/routes.mjs`, `src/templates.mjs`, `src/source-rewrites.mjs`, `run-command.mjs`, `config.json`, and `ecosystem.config.js` MUST remain unchanged.
5. `src/server.mjs` MUST continue to derive routes, configuration, and the Wisp prefix from `src/routes.mjs`, and MUST continue to render fallback error pages through `src/templates.mjs`.
6. The `fastify`, `@fastify/helmet`, and `@fastify/static` dependencies MUST be removed from `package.json`, since they have no uses outside the migrated scope; no environment-provided dependency outside the migrated scope may be removed.
7. The client proxy assets (`views/sw.js`, `views/sw-blacklist.js`, `views/uv/`, `views/scram/`), the build and template sources listed in the Goal, and the `config.json`/`ecosystem.config.js` contracts MUST NOT be modified or deleted.
8. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
9. Work in `/workspace/repo`.
10. The migrated server MUST build from `/workspace/repo` by running `pnpm run build`, which compiles the `views/dist` artifacts from `views/` and the bundled client libraries; the server cannot serve pages without it.
11. With the server running, `pnpm test` (alias `pnpm run proxy-validator`) MUST run `proxyServiceValidator.js`, an integration check that loads pages through a headless browser and confirms the service responds, and `pnpm run workflow-test` MUST start a temporary server to verify it boots without startup errors; these public checks exercise end-to-end behavior but do not by themselves prove every requirement above.

IMPORTANT: Create a new branch from the task's base commit 3d8e4b8089233b343eda2e9694483df3b57f9ce6 and commit all task changes, including new files, when you are done.
