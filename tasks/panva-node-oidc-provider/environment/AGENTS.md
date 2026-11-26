# Project instructions for this task (migration acceptance)

Use the task instruction as the acceptance contract. Migrate the provider's
registered HTTP routes from Koa to Fastify 5.12.x, including dispatch, request
and response access, cookies, persistence timing, CORS, bodies, errors and proxy
detection. Fastify must execute the live request and response lifecycle.

Preserve endpoint behavior, constructor configuration, exports, adapter payloads,
events, cookie contracts and callback/listen mounting, including `/oidc` prefixes.
Keep Fastify a runtime dependency bounded to 5.12.x. Koa packages may remain as
development dependencies for examples and tests.

The Koa-typed extension surface (`Provider#use`, `Provider.ctx`,
`provider.createContext`, `provider.keys`, `provider.middleware`), koa-mount
semantics, deprecated `.app`, templates and browser assets are out of scope.
No particular context-factory name or argument layout is an acceptance condition.
A context adapter may bridge existing actions to Fastify request/reply objects;
it must not delegate the migrated HTTP responsibilities to Koa.

Limit changes to the migration scope, dependency configuration and necessary
public mounting wiring. Preserve test cases, assertions and parity comparisons.
Run the public validation commands from the task instruction; never skip or
weaken checks to make the migration pass.
