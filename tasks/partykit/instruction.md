# Migrate the `hono-party` adapter from Hono to Elysia

## Background

`packages/hono-party` provides a server-side adapter between an HTTP framework and PartyServer. At the starting revision `f0a2e97d233f24545b2648aec2ed6a191e11074e`, `packages/hono-party/src/index.ts` imports helpers from `hono/adapter` and `hono/factory` and exports `partyserverMiddleware`. The package is being migrated from Hono 4.12.15 to Elysia 1.3.0.

`packages/hono-party/README.md` documents that `onBeforeConnect` may return a `401` Response to reject a WebSocket connection. The starting `handleWebSocketUpgrade` falls through to the next handler when `routePartykitRequest` returns a Response without a `webSocket` handle, which discards that rejection. The migrated adapter MUST return such a non-null Response as the final response.

## Goal

- Migrate the adapter in `packages/hono-party/src/index.ts` from Hono to Elysia 1.3.0.
- Update `packages/hono-party/package.json` for the new dependency.
- Elysia MUST provide middleware registration, request and context propagation, and error-flow handling for the adapter.
- `partyserver` MUST remain the routing layer through `routePartykitRequest`.
- Other packages, fixtures, documentation, browser code, and build configuration are out of scope.

## Public contract to preserve

- `partyserverMiddleware(ctx?)` MUST remain the package root export.
- The returned value MUST be registerable on an Elysia app through `use`.
- When registered before the application's routes, the middleware MUST run before those handlers.
- The package MUST keep the name `hono-party` and continue to expose `partyserverMiddleware` from its root export.
- `ctx.options` MUST accept the existing `PartyServerOptions` fields, including `prefix`, `cors`, `jurisdiction`, `locationHint`, `props`, and `routingRetry`.
- The adapter MUST forward those options to `routePartykitRequest`.
- `ctx.onError` MUST remain an optional callback that receives the thrown error.
- The exported options type MUST continue to describe `onBeforeConnect` and `onBeforeRequest` with `(request, lobby, context)` parameters.
- The third callback parameter MUST provide access to the current request's environment bindings as `env` and to the current Elysia request context.
- The function MUST continue to accept an optional type argument that lets callers type the third parameter and its environment bindings.
- Hono-only helpers such as `c.var` and `c.get()` are not part of the preserved contract.

## Behavior requirements

- A request is an upgrade request when its `Upgrade` header equals `websocket` case-insensitively.
- `options.prefix` MUST limit the paths handled by PartyServer. Requests outside that prefix MUST pass through without being intercepted.
- For a non-upgrade request whose path matches the PartyServer prefix, the adapter MUST invoke `onBeforeRequest` when configured and MUST NOT invoke `onBeforeConnect`.
- For an upgrade request whose path matches the PartyServer prefix, the adapter MUST invoke `onBeforeConnect` when configured and MUST NOT invoke `onBeforeRequest`.
- The adapter MUST forward the incoming request to `routePartykitRequest` with the current environment bindings and configured options.
- When forwarding a request, the adapter MUST preserve its URL, method, headers, and body.
- If `routePartykitRequest` returns a non-null Response for a non-upgrade request, the adapter MUST return that Response with its status, headers, and body unchanged.
- If `routePartykitRequest` returns `null` for a non-upgrade request, the adapter MUST pass the request to the next Elysia handler and return that handler's response.
- If `routePartykitRequest` returns a Response with a `webSocket` handle for an upgrade request, the adapter MUST return a `101` Response carrying that handle and MUST NOT pass the upgrade to later handlers.
- If `routePartykitRequest` returns a non-null Response without a `webSocket` handle for an upgrade request, including a Response returned by `onBeforeConnect`, the adapter MUST return that Response and MUST NOT call later handlers.
- If `routePartykitRequest` returns `null` for an upgrade request, the adapter MUST pass the request to the next Elysia handler and return that handler's response.
- If `onBeforeConnect` returns a `Request`, the adapter MUST forward that replacement request. If it returns `void`, the adapter MUST forward the original request.
- If `onBeforeConnect` returns a `Response`, the adapter MUST use it as the final response and MUST NOT continue to later handlers, even when the Response has no `webSocket` handle.
- If `onBeforeRequest` returns a `Request`, the adapter MUST forward that replacement request. If it returns `void` or `undefined`, the adapter MUST forward the request it received, including any in-place modifications such as added headers, so those modifications affect the forwarding result.
- If `onBeforeRequest` returns a `Response`, the adapter MUST return it and MUST NOT continue to later handlers.
- If processing throws and `ctx.onError` is present, the adapter MUST invoke `onError` with the thrown error. It MUST then pass the request to the next Elysia handler and return that handler's response.
- If processing throws and `ctx.onError` is absent, the adapter MUST let the error propagate.
- Errors thrown by `onBeforeConnect` or `onBeforeRequest` MUST follow the same `onError` behavior.

## Constraints

1. The migration MUST NOT remove required public capabilities or change their contracts outside the stated goal.
2. Production code in `packages/hono-party/src` MUST NOT import or call Hono for the migrated responsibilities.
3. Production code MUST NOT delegate the migrated responsibilities to a Hono runtime through a subprocess, RPC, FFI, dynamic import, or wrapper.
4. Changes MUST be limited to `packages/hono-party/src/index.ts`, `packages/hono-party/package.json`, and test files and test configuration under `packages/hono-party`.
5. `packages/hono-party/package.json` MUST remove `hono` from peer and dev dependencies.
6. `packages/hono-party/package.json` MUST declare `elysia` with a version constraint that includes 1.3.0 in both peer and dev dependencies.
7. `packages/hono-party/package.json` MUST keep the existing `partyserver` peer dependency range.
8. Do not change PartyServer Durable Object routing, lifecycle, hibernation, SQL schema, connection attachments, or `__ps_name` semantics.
9. Do not change the observable behavior of `routePartykitRequest`, including unmatched `null`, 404 responses, exceptions, `onError`, or `onException`.
10. Do not migrate or rewrite React, browser, client-rendering, page, component, or UI examples.
11. Do not add or modify frontend build tooling, CSS or UI component libraries, or client state management dependencies.
12. Do not modify `packages/partyserver`, other workspace packages, fixtures, or documentation.
13. Do not delete, weaken, or bypass existing tests. Do not skip checks, remove assertions, or hardcode evaluation inputs to fake compliance.
14. If you add or adapt tests, change only test plumbing required by the target framework. Preserve all behavioral assertions.
15. Work from the provided baseline snapshot in `/workspace/repo` at commit `f0a2e97d233f24545b2648aec2ed6a191e11074e`. Do not substitute `main` or another branch.
16. Hono dependencies in out-of-scope fixtures may remain.

## Validation

From `/workspace/repo/packages/hono-party`, run `npm run build`.

This command compiles the migrated adapter and emits its type declarations. It does not exercise WebSocket or HTTP behavior at runtime.

Changes are collected as a patch of the repository working tree against baseline commit `f0a2e97d233f24545b2648aec2ed6a191e11074e`. No new branch or commit is required.
