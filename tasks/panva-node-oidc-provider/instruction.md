# Migrate the oidc-provider request layer from Koa to Fastify

## Background

`oidc-provider` is an OAuth 2.0 / OpenID Connect authorization server. It publishes only the `lib/` directory and is ESM only (`"type": "module"` in `package.json`), and it requires a Node.js LTS runtime (`lib/index.js` warns on unsupported runtimes).

The package currently uses Koa 3.2.1 as its underlying HTTP framework. `Provider` extends Koa, the endpoint dispatcher is mounted as a single Koa middleware, and routing, request and response access, cookies, sessions, CORS, request-body reading, and error rendering are expressed in Koa context (`ctx`) terms.

## Goal

- Migrate the server-side HTTP framework used by `oidc-provider` from Koa 3.2.1 to Fastify 5.12.0.
- Fastify MUST own the migrated responsibilities for every route the provider registers: request dispatch and route matching, request and response access, cookie reading and writing, session and interaction persistence timing, CORS response headers, request-body reading, error rendering, and secure/proxy detection.
- `Provider` MUST NOT be a Koa application or extend Koa, and production code in the migration scope MUST NOT require Koa to serve a request.
- The migration scope is these files:
  - `lib/provider.js`
  - `lib/index.js`
  - `lib/helpers/initialize_app.js`
  - `lib/helpers/router.js`
  - `lib/helpers/oidc_context.js`
  - `lib/helpers/defaults.js`
  - `lib/shared/cors.js`
  - `lib/shared/error_handler.js`
  - `lib/shared/session.js`
  - `lib/shared/selective_body.js`
  - `lib/actions/interaction.js`
  - `lib/actions/authorization/resume.js`
  - `lib/actions/end_session.js`
  - `lib/response_modes/web_message.js`
  - `lib/models/session.js`
- The following are out of scope and MUST NOT be migrated:
  - the Koa-typed extension surface: `Provider#use(ctx, next)`, the static `Provider.ctx`, `provider.createContext`, `provider.keys`, and `provider.middleware`;
  - `koa-mount`-derived `ctx.mountPath` semantics and the deprecated `.app` getter;
  - server-rendered interaction templates (`tools/views/*.eta`, `lib/views`) and browser-side assets.

## Public contract to preserve

- `new Provider(issuer, configuration)` keeps its constructor contract and the configuration/feature schema, including `features`, `ttl`, `clientAuthMethods`, `jwks`, `responseTypes`, `subjectTypes`, `pairwiseIdentifier`, `adapter`, and `cookies.keys`.
- The HTTP behavior of every endpoint the provider registers stays the same: `authorization`, `resume`, `userinfo`, `token`, `jwks`, discovery, `openid_credential_issuer`, `credential`, `challenge`, `registration` and `client`, `revocation`, `introspection`, `end_session`, `device_authorization`, `code_verification`, `pushed_authorization_request`, `backchannel_authentication`, and `interaction`.
- `provider.callback()` MUST return a Node `(req, res)` request handler that can be passed to `http.createServer` and mounted in an Express, Fastify, or Hapi host. `provider.listen(...)` MUST keep working.
- The named exports stay the same: the default `Provider`, `Provider`, `errors`, `interactionPolicy`, and `ExternalSigningKey`.
- The adapter persistence contract and the persisted payload shape stay the same (`upsert`, `find`, `findByUid`, `findByUserCode`, `consume`, `destroy`, `revokeByGrantId`).
- The provider events stay the same: `access_token.saved|issued|destroyed`, `authorization_code.consumed|saved|destroyed`, `authorization.accepted`, `interaction.*`, `session.*`, and `registration.*`.
- Interaction and session cookie names, their signing and rotation attributes, and the interaction UID cookie contract stay the same.

## Behavior requirements

- The authorization endpoint MUST run its protocol checks in the existing order: `assignDefaults`, `checkPrompt`, `checkScope`, `checkOpenidScope`, `checkRedirectUri`, `checkPKCE`, `checkClaims`, `checkRar`, `checkResource`, `checkMaxAge`, `checkRequestedExpiry`, `checkCibaContext`, `checkIdTokenHint`, `checkDpopJkt`, `checkExtraParams`, `loadAccount`, `loadGrant`, `interactions`, then `respond`.
- Endpoint-scoped and per-profile CORS headers and statuses MUST keep their current values for the `open`, `challenge`, `userinfo`, `client`, `clientWithDPoP`, and `credential` profiles. Exposed headers MUST still include `DPoP-Nonce`, `OAuth-Client-Attestation-Challenge`, and `WWW-Authenticate`. Preflight `OPTIONS` requests MUST still receive `204`.
- Error response bodies and the `WWW-Authenticate` header construction MUST stay the same per endpoint, including `Bearer realm="..."` values, `SessionNotFound` handling, and the generic `server_error` body.
- The `query`, `fragment`, `form_post`, `jwt`, and `web_message` response modes MUST keep their serialization and parameter placement.
- Request bodies for `application/x-www-form-urlencoded` and JSON MUST be parsed as before. A body that cannot be parsed MUST still produce the `400` response with `error` equal to `failed to parse the request body`. An unsupported content type on an endpoint that reads a body MUST keep its current rejection.
- When `provider.proxy` is `true`, the provider MUST trust `X-Forwarded-Proto` when deciding whether a request is secure, so absolute URLs such as the discovery document use `https`.
- Route matching MUST keep the behavior actions rely on: `HEAD` is routed to the `GET` handler, a route name resolves to its first registration, and the request exposes the `params` and router fields the actions consume.

## Constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production code within the migration scope MUST NOT import or call Koa for the migrated responsibilities.
3. Production code within the migration scope MUST NOT delegate the migrated responsibilities to a Koa runtime, whether through a child process, RPC, FFI, or dynamic loading.
4. Changes MUST be limited to the migration-scope files, the dependency configuration in `package.json`, and the minimal public test wiring needed to mount the provider.
5. `fastify` MUST be a runtime dependency at the pinned 5.12.x line. Koa packages may remain installed as devDependencies only for out-of-scope examples and tests.
6. The public test suites under `test/`, their fixtures, and the public exports of `lib/index.js` are protected. This includes the parity suites that hold the transcribed router, CORS, and body behavior to the libraries they replaced: `test/router/router_parity.test.js`, `test/cors/cors_parity.test.js`, and `test/body_parser/raw_body_parity.test.js`. Public test wiring may be adapted only where the framework change makes the old mounting impossible, and its assertions, coverage, and comparisons MUST be preserved.
7. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs to fake compliance.
8. The provider MUST keep working when mounted through `provider.callback()`, including under a path prefix such as `/oidc`.
9. Work in `/workspace/repo`. The task starts from commit `8a5917001cbee9d328f7854043d36ed8a03f360e`; keep that starting point and do not substitute another branch. Your repository changes are collected as a patch against that commit.

## Validation

From `/workspace/repo`, run `npm test` (`node ./test/run`). It mounts the provider through `provider.callback()` on a plain Node HTTP server and exercises the endpoint behavior. `npm run test-ci` repeats the suite for the default mount and for Express, Koa, Hapi, and Fastify hosts mounted at `/oidc`. `npm run test-dist` builds `dist/`, packs it, imports every published module with only the declared runtime dependencies, and runs the suite against the packed `lib/`. `npm run lint` runs the Biome lint.

These public checks are necessary but do not by themselves prove every requirement above.
