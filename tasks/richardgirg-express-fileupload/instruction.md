# Migrate express-fileupload to Koa 2 Middleware

## Background

`express-fileupload` 1.5.2 is multipart upload middleware for Node.js. It wraps Busboy and targets Express 4.21.2 with the signature `(req, res, next)`. It attaches form fields to `req.body` and uploaded files to `req.files`.

Connection abortion, lifecycle handling, and errors currently follow Express callbacks and direct Node response writes. Koa 2.16.4 applications use an asynchronous pipeline with the signature `async (ctx, next)`.

## Goal

- Migrate the middleware factory so it runs natively on Koa 2.16.4 and still supports Express 4.21.2.
- On Koa, the middleware MUST use `async (ctx, next)`. It MUST map form fields to `ctx.request.body` and uploaded files to `ctx.request.files`.
- `ctx.req.body` MUST be the same object as `ctx.request.body`. `ctx.req.files` MUST be the same value as `ctx.request.files`.
- The Express 4 path MUST keep the 3-argument signature `(req, res, next)`.
- Multipart parsing and temporary-file persistence for a request MUST finish before `await next()`.
- Temporary files created for a request MUST be removed before the middleware resolves or rejects on errors, limits, timeouts, or stream aborts.
- Koa parsing, attachment, limit handling, and cleanup MUST NOT be delegated to the Express runtime.

## Public contract to preserve

### 1. Middleware factory and options

The exported factory `fileUpload(options)` MUST keep these options and defaults:

- `debug` defaults to `false`. `logger` defaults to `console`.
- `uploadTimeout` defaults to `60000` milliseconds.
- `useTempFiles` defaults to `false`. `tempFileDir` defaults to `path.join(process.cwd(), 'tmp')`. `tempFilePermissions` defaults to `0o644`.
- `parseNested` defaults to `false`.
- `uriDecodeFileNames` defaults to `false`. `safeFileNames` defaults to `false`. `preserveExtension` defaults to `false`.
- `abortOnLimit` defaults to `false`. `responseOnLimit` defaults to `'File size limit has been reached'`. `limitHandler` defaults to `false`, which means no custom handler.
- `createParentPath` defaults to `false`. `hashAlgorithm` defaults to `'md5'`.

### 2. File object structure and methods

Every parsed file object on `ctx.request.files`, on `ctx.req.files` in Koa mode, and on `req.files` in Express mode MUST provide:

- `name`: the transformed filename string.
- `data`: a `Buffer` of the uploaded content when `useTempFiles` is `false`, or an empty `Buffer` when `useTempFiles` is `true`.
- `size`: the uploaded size in bytes.
- `encoding`: the transfer encoding string reported by Busboy.
- `mimetype`: the MIME type string reported by Busboy.
- `truncated`: a boolean that is true when the file hit the size limit.
- `tempFilePath`: the written temporary-file path when `useTempFiles` is `true`, otherwise `''`.
- `md5`: the checksum string computed with `hashAlgorithm`.
- `mv(destinationPath, [callback])`: moves the file to `destinationPath`. It returns a `Promise` when `callback` is omitted. It calls `callback(err)` when `callback` is supplied. It creates parent directories when `createParentPath` is `true`.

## Behavior requirements

### 1. Koa 2 middleware integration

- When used in a Koa 2.16.4 pipeline as `async (ctx, next)`, the middleware MUST return a Promise.
- The middleware MUST skip multipart processing and call `await next()` for a non-multipart request.
- It MUST do the same for methods `GET`, `HEAD`, `DELETE`, `OPTIONS`, `CONNECT`, and `TRACE`.
- It MUST do the same when `Transfer-Encoding` is absent and `Content-Length` is missing or `'0'`.
- It MUST parse a request that has a positive `Content-Length` or `Transfer-Encoding: chunked`.
- Form fields MUST be set on `ctx.request.body` and `ctx.req.body`. Fields that share a name MUST accumulate into an array.
- When a request has no form fields, both body properties MUST be `{}`, not `undefined` or `null`, even if `ctx.request.body` was set before this middleware.
- Uploaded files MUST be set on `ctx.request.files` and `ctx.req.files`. Files that share a field name MUST accumulate into an array.
- When no valid files are uploaded, both file properties MUST be `null`, including when `parseNested` is `true`.
- An empty filename with 0 bytes MUST NOT produce a file object. In temporary-file mode, any temporary artifact created for that empty file MUST be removed immediately.
- When `parseNested` is `true`, keys such as `user[name]` and `user[avatar]` MUST become nested objects on both `body` and `files`.
- After a successful upload, `await next()` MUST run exactly once after all file data is received and any temporary files are flushed to disk, even if an upstream response was already assigned or sent.

### 2. Express 4 compatibility

- With `(req, res, next)`, the middleware MUST keep Express 4 behavior. It MUST set `req.body` and `req.files`, then call `next()` or `next(err)`.
- The existing Express suite MUST continue to pass.

### 3. File size limit and abortion

- A file reaches the limit when Busboy emits `'limit'` at `limits.fileSize`.
- When `abortOnLimit` is `false`, the file MUST have `truncated: true`. Parsing MUST continue, and downstream middleware MUST run exactly once after parsing, persistence, and any `limitHandler` have completed successfully. This also applies if a response was already assigned or sent before or during the upload.
- When `abortOnLimit` is `true`, parsing MUST stop. The request stream MUST be unpiped and then drained or closed. Further application parsing MUST NOT continue.
- Every aborted or failed upload MUST have its remaining request-owned temporary files removed and its stream resources released before the Koa middleware Promise settles or an Express terminal error is forwarded. When the upload middleware itself ends a new abort/error response, this cleanup MUST finish before it ends that response. If upstream code or a user handler has already sent or ended the response, cleanup remains mandatory at the middleware/continuation boundary; it is not required to precede that earlier response event and MUST NOT rewrite or reopen the response.
- For a non-aborting Koa upload that reaches a size limit, downstream MUST be allowed to use the parsed files first. After downstream finishes or rejects, remove the remaining request-owned temporary files and release stream resources before the middleware Promise settles, as specified under "Successful completion and terminal failure".
- A configured `limitHandler` MUST be invoked when a file reaches the limit, regardless of `abortOnLimit`. In Koa mode, a handler of 2 or fewer parameters MUST receive `(ctx, noopNext)`. A handler of 3 parameters MUST receive `(ctx.req, ctx.res, noopNext)`. `noopNext` MUST NOT run the real downstream middleware.
- In Express mode, `limitHandler(req, res, next)` MUST keep its existing callback behavior.
- A Promise returned by `limitHandler` MUST be awaited. A thrown or rejected error MUST propagate to the caller.
- When `abortOnLimit` is `true`, the real downstream application middleware MUST NOT run.
- When `abortOnLimit` is `true` and the limit is reached, the middleware MUST NOT overwrite a response that is already committed. The response counts as committed when `ctx.res.headersSent` or `ctx.headerSent` is true, when `ctx.status` differs from its value at the start of the upload, or when `ctx.body` was already assigned — regardless of whether the write came from upstream Koa middleware (for example a middleware that calls `ctx.res.writeHead()` and then `await next()`), from `limitHandler`, or from an earlier write. A committed response MUST NOT be overwritten and the middleware MUST NOT call `await next()` in that case. This rule applies whether or not `limitHandler` is configured.
- If the response is not committed, the middleware MUST set `ctx.status` to `413` and `ctx.body` to `options.responseOnLimit` after `limitHandler` has run, if any. It MUST NOT call `await next()`.

### 4. Errors and cleanup

- On upload timeout, Busboy parse error, disk write failure, or request abortion, the middleware MUST stop parsing, release stream resources, and delete every temporary file created for that request before its Promise resolves or rejects.
- If an upload exceeds `uploadTimeout`, the middleware MUST propagate an `Error` whose message begins with `Upload timeout for `. It MUST clean temporary files and abort further stream processing.
- After temporary cleanup, a Busboy error or filesystem write failure MUST reject the middleware Promise in Koa, or call `next(err)` in Express.
- A synchronous failure while preparing `tempFileDir`, including `mkdir` errors `ENOTDIR` and `EACCES`, MUST follow that same path. It MUST NOT become an uncaught exception, and downstream middleware MUST NOT run.
- An error thrown by `await next()` MUST propagate. It MUST NOT be swallowed and MUST NOT cause a second cleanup error. Temporary files for that request MUST be deleted before the middleware Promise rejects.

The following example is illustrative, not a required implementation:

```javascript
const Koa = require('koa');
const request = require('supertest');
const fileUpload = require('./lib/index');

const app = new Koa();
app.use(fileUpload({ useTempFiles: true }));

app.use(async (ctx) => {
  if (ctx.path === '/upload') {
    const file = ctx.request.files && ctx.request.files.avatar;
    ctx.status = file ? 200 : 400;
    ctx.body = {
      username: ctx.request.body && ctx.request.body.username,
      filename: file ? file.name : null,
      tempFilePath: file ? file.tempFilePath : null
    };
  }
});

request(app.callback())
  .post('/upload')
  .field('username', 'alice')
  .attach('avatar', Buffer.from('image content'), 'avatar.png')
  .end((err, res) => {
    if (err) throw err;
    console.log('Upload result:', res.status, res.body);
  });
```

## Constraints

1. Work in `/workspace/repo` on Node.js 18.20.8. Koa integration MUST target Koa 2.16.4. Express compatibility MUST target Express 4.21.2.
2. Production changes MUST stay inside `lib/`. Public utilities in `lib/utilities.js` MUST NOT change. You MUST NOT create, modify, or delete any file outside `lib/`; scratch or ad-hoc validation scripts (for example a new `validation/` directory) are not permitted. Any extra Koa checks MUST be run inline (for example `node -e '...'`) without writing new files into the repository.
3. You MUST NOT modify `test/`, fixtures, CI configuration, or packaging files.
4. You MUST NOT add, remove, or upgrade runtime dependencies. `busboy` MUST stay at its current range. `koa` and `supertest` are already available for tests and MUST NOT be added to `package.json` `dependencies`.
5. You MUST NOT replace Busboy, add browser or UI features, or do unrelated refactoring.
6. You MUST NOT satisfy the Koa path by calling Express, spawning a process, or dynamically loading the unmigrated Express middleware.
7. You MUST NOT skip checks, remove assertions, weaken comparisons, or hardcode evaluation inputs.

## Implementation and validation guidance

This section makes the task's request-eligibility and Express `limitHandler` callback decisions explicit, and suggests checks for the other requirements. It does not prescribe an internal architecture, storage backend, middleware return value in Express mode, or cleanup rule for an ordinary successful upload.

### Request eligibility comes before parsing

- Evaluate method, request-body framing, and multipart-header eligibility together before starting Busboy. A positive `Content-Length` or `Transfer-Encoding: chunked` is not sufficient by itself; the framing rule does not override the other eligibility rules in Behavior requirements 1.
- After the method and framing checks, Koa must admit either a request accepted by the frozen Express eligibility check or a multipart header whose parsed parameters declare a boundary. Keep the frozen eligibility behavior for Express. A bare `multipart/form-data` header bypasses; `multipart/form-data; note="boundary=wrong"` enters parsing through the frozen-compatibility branch and reports Busboy's missing-boundary error. A non-multipart media type containing `boundary=` still bypasses. For a bypass, no parser starts, existing body/files remain unchanged, and downstream is awaited. An admitted malformed request propagates its parser error and cleans up instead of continuing successfully.
- In Koa mode, also recognize multipart syntax with media-type and parameter casing, quoted boundaries, and whitespace that Busboy accepts. Declared but empty boundaries enter parsing and fail there. Neither a MIME-type-only check nor a literal `boundary=` substring check alone implements the two eligibility branches.

### Koa and Express continuation are different

A shared multipart parser is allowed, but its adapters have different observable contracts. Do not assume the existing Express guard implements all Koa eligibility rules.

| Checkpoint | Koa mode | Express mode |
| --- | --- | --- |
| Application continuation | Await the real `next()` Promise. | Preserve callback arguments to `next()`, `next(err)`, and routing controls such as `next('route')` or `next('router')`. |
| Limit-handler continuation | Pass `noopNext`; it is not the application continuation. | Preserve the existing `limitHandler(req, res, next)` callback semantics. |
| Error boundary | Reject the middleware Promise after required cleanup. | Forward the error through the application callback after required cleanup. |
| Completion evidence | Check parsing, persistence, handler completion, and downstream completion separately. | A callback invocation alone does not prove that upload parsing or persistence has finished. |

- Keep ordinary continuation, routing controls, and error propagation distinguishable; do not reduce all callback arguments to a boolean. A blanket "next already called" guard can discard an error that still needs propagation, while forwarding every callback immediately can expose incomplete upload data or cause repeated application dispatch.
- A handler returning `undefined`, its returned Promise settling, its callback being invoked, and multipart parsing finishing are different events. Check their timing independently. Do not treat Express `next` as a Koa Promise or turn a thrown/rejected error into successful routing.
- For Express `limitHandler` callback/Promise mixtures and competing errors, use the selection and completion rules below. Other Express behavior still follows the frozen compatibility contract.

### Express limit-handler callback selection

- An Express `limitHandler` may call its supplied `next` more than once, before or after returning. Forward at most one result to the real application `next` for that request. Do not dispatch the application once per callback.
- A callback with a falsy value (including no argument, `null`, `false`, `0`, or `''`) requests ordinary continuation. `'route'` and `'router'` are routing controls. Any other truthy callback value requests error propagation; preserve the original value and object identity without coercing it to an `Error` or probing arbitrary properties.
- A value thrown by a handler or used to reject its returned Promise is a handler failure, not a callback result. Preserve the identity of a native `Error`, including one from another JavaScript realm. Convert another value to an `Error` without letting conversion itself escape: use its safely obtained string as the message when possible, and use `Unknown multipart error` when the value is falsy or cannot be safely converted. Thus a thrown or rejected `'route'` becomes `new Error('route')`; it is not a routing control. Do not inspect arbitrary error metadata, prototypes, or `then` properties merely to classify a callback value.
- For a non-aborting upload, an error callback takes precedence over routing and ordinary callbacks; preserve the first callback error when later callbacks or the same handler's Promise report another failure. Otherwise the first routing control takes precedence over earlier ordinary callbacks and later routing controls. With neither error nor routing control, dispatch ordinary `next()` once. Finish parsing, file persistence, and successful handler Promises before ordinary or routing dispatch, including when a callback ran during a partial upload. When `abortOnLimit` is `true`, ordinary and routing callbacks do not dispatch downstream; callback errors still propagate.

### Successful completion and terminal failure

- For a non-aborting Koa upload, complete parsing, file persistence, and any returned `limitHandler` Promise before invoking the real downstream middleware. Await the downstream Promise even if a response is already assigned or sent.
- For a non-aborting Koa upload that reached a limit, let downstream use the parsed files first. After downstream finishes or rejects, remove that request's remaining temporary files and release its stream resources before the middleware settles. Response assignment or completion does not by itself mean downstream has finished, permit early deletion of files still in use, or waive required cleanup. A file moved by downstream must not cause its destination or another request's files to be deleted.
- A non-aborting size limit does not mean the multipart request has completed. Continue handling the rest of the request and keep incomplete uploads subject to `uploadTimeout`. After a file is truncated, raw request chunks can still arrive without further `file` `data` events: continued request activity must prevent a false timeout, while a stalled request must still time out.
- An Express callback error from a `limitHandler` may arrive before multipart parsing finishes or after that handler returned. Record the selected callback error immediately, but do not stop parsing merely because the callback ran while that handler's returned Promise is still pending. Parsing and persistence may continue during this wait, so fields and files already arriving after the limit are not discarded. When the error-owning handler settles, stop any still-incomplete parsing, complete request-owned cleanup, and forward the selected error once; do not wait indefinitely for the entire multipart request or for an unrelated handler's pending Promise. A later callback, handler rejection, or handler completion must not replace the selected first callback error. An already committed response does not skip these parsing, handler, cleanup, or Express error-callback rules.
- A request-stream error, request abort, or upload timeout interrupts the callback-error wait: stop parsing, clean up, and propagate that terminal event once even when the error-owning handler remains pending. Afterward, settling that handler must not produce a second application callback.
- On an error or abort, stop parsing, unpipe and drain or close the request as applicable, and finish request-owned cleanup before propagating the original error. Calling `destroy()`, observing a write `finish`, or removing a pathname alone is not proof that stream resources have been released. Do not require a particular filesystem API or a particular order of unlink and close; verify the actual completion.
- Distinguish genuine parser, disk-write, handler, and downstream failures from secondary errors caused by intentional shutdown. Register and retain the stream-error and close bookkeeping needed to observe actual writer release before intentionally destroying active writers. A write rejection caused by that intentional size-limit shutdown, such as write-after-destroy, must not turn a correctly handled abort into a new failure: preserve the normal `413` or already-committed handler response and settle only after cleanup. Base this decision on ownership and shutdown state, not solely on an error code; unrelated write failures must still propagate.

### Suggested self-checks and observations

Use the existing Express suite plus inline checks with real Koa and Express. Cover memory and temporary files, ordinary and limited uploads, both `abortOnLimit` values, synchronous and Promise-returning handlers, already-committed responses, and downstream errors. Observe the following separately:

| Scenario | What to observe |
| --- | --- |
| Eligibility and malformed headers | Methods and framing; bare multipart bypass versus an admitted header with a `boundary=` token only inside another parameter; casing, quoting, empty boundaries, and whitespace; parser entry; existing body/files on bypass; error propagation. |
| Callback during a partial upload | Whether later fields and file contents are available when application continuation occurs; parsing and persistence completion versus callback and handler completion. |
| Ordinary, routing, and error callbacks | First ordinary callback followed by routing controls and an error; only one application dispatch; preserved callback value and identity; thrown/rejected strings and opaque values converted as specified; returned-Promise settlement; error after handler return. |
| Multiple limited files | Different handler completion orders; first callback error's owner pending versus finished; parsing progress while its owner is pending; another handler pending or rejecting; stream termination interrupting the wait; propagated error identity and request-owned cleanup. |
| Truncated file with an open request | More raw request chunks arriving after the file limit versus a stalled request; correct timeout behavior and a single downstream call on success. |
| Abort while a temporary write is active | A normal `413` or committed handler response without a leaked shutdown error; actual stream-resource release before settlement; remaining temporary paths; genuine write failures still propagate. |
| Downstream failure or file movement | Original error propagation; cleanup after downstream; moved destinations and other requests' files remain untouched. |

Use controllable Promises and request chunks to create partial-upload and pending-handler checkpoints, rather than relying on long sleeps. Always release held work and close test resources in teardown. Include a watchdog, a nonzero failure exit, and an explicit success marker so a never-settling Promise cannot look like a passing check.

The following small in-memory smoke check runs inline without creating repository files or listening on a network port. It checks valid header/framing variants, aliases, repeated fields, file contents, and awaiting downstream. It does not cover bypass, malformed requests, Express callback arbitration, or the full lifecycle matrix. Neither this smoke check nor `npm test` alone proves that the migration is complete.

Koa 2.16.4 is preinstalled at `/opt/koa-test-deps/node_modules`. The environment sets `NODE_PATH` to that directory, so `require('koa')` works directly from `/workspace/repo`. `supertest` is available in the repository's existing development dependencies. Do not install dependencies or modify packaging files. If Koa cannot be resolved, verify `NODE_PATH=/opt/koa-test-deps/node_modules` and report an environment problem rather than substituting a mocked Koa context.

```bash
node <<'NODE'
const assert = require('assert');
const http = require('http');
const { Socket } = require('net');
const { Readable } = require('stream');
const Koa = require('koa');
const fileUpload = require('./lib');
const watchdog = setTimeout(() => {
  console.error('Koa smoke check did not complete');
  process.exit(1);
}, 5000);

async function smoke() {
  const boundary = 'public-smoke-boundary';
  const payload = Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="tag"\r\n\r\none\r\n`
    + `--${boundary}\r\nContent-Disposition: form-data; name="tag"\r\n\r\ntwo\r\n`
    + `--${boundary}\r\nContent-Disposition: form-data; name="upload"; filename="sample.txt"\r\n`
    + `Content-Type: text/plain\r\n\r\nhello\r\n--${boundary}--\r\n`
  );
  const contentTypes = [
    `multipart/form-data; boundary=${boundary}`,
    `Multipart/Form-Data; BOUNDARY="${boundary}"`
  ];
  for (const contentType of contentTypes) {
    for (const framing of ['length', 'chunked']) {
      const req = Readable.from([payload]);
      req.method = 'POST';
      req.url = '/upload';
      req.complete = true;
      req.headers = { 'content-type': contentType };
      if (framing === 'length') req.headers['content-length'] = String(payload.length);
      else req.headers['transfer-encoding'] = 'chunked';
      req.socket = new Socket();
      const ctx = new Koa().createContext(req, new http.ServerResponse(req));
      let downstreamCalls = 0;
      let downstreamFinished = false;
      try {
        const result = fileUpload()(ctx, async () => {
          downstreamCalls += 1;
          assert.strictEqual(ctx.req.body, ctx.request.body);
          assert.strictEqual(ctx.req.files, ctx.request.files);
          assert(ctx.request.body && ctx.request.files, 'multipart data must be available to downstream');
          assert.deepStrictEqual(ctx.request.body.tag, ['one', 'two']);
          assert.strictEqual(ctx.request.files.upload.data.toString(), 'hello');
          await new Promise((resolve) => setTimeout(resolve, 10));
          downstreamFinished = true;
        });
        assert(result && typeof result.then === 'function');
        await result;
        assert.strictEqual(downstreamCalls, 1);
        assert.strictEqual(downstreamFinished, true);
      } finally {
        req.destroy();
        req.socket.destroy();
      }
    }
  }
  console.log('Koa smoke checks passed');
}

smoke().then(() => clearTimeout(watchdog), (error) => {
  clearTimeout(watchdog);
  console.error(error);
  process.exitCode = 1;
});
NODE
```

## Validation

From `/workspace/repo`, run `npm test`.

That command checks the existing Express suite only. It does not prove the Koa behavior above.

Create a new branch from the task base commit `20269154bb9aab9672e7ff7ca500349e5d23fb5c` and commit all task changes, including new files, when you are done.
