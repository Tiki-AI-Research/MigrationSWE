'use strict';

// Exercise the actual hidden harness on Node 14 without opening sockets or
// executing a candidate. Fake stream events cover failures that otherwise
// consume the full outer verifier timeout.
const assert = require('assert');
const { EventEmitter } = require('events');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const patch = fs.readFileSync(process.argv[2] || path.join(__dirname, 'test.patch'), 'utf8');
const harness = patch.split('\ndiff --git ')[0].split('\n')
    .filter((line) => line.startsWith('+') && !line.startsWith('+++'))
    .map((line) => line.slice(1)).join('\n');

function environment(workerSpec = {}) {
    const timers = new Set();
    const requests = [];
    const agents = [];
    let constructionError;
    let writeError;
    let closeCount = 0;
    const output = [];
    const exitCodes = [];
    const app = {
        start(options) {
            options.onStart();
            return {
                app: { server: { address: () => ({ port: 1234 }) } },
                async close() { closeCount += 1; },
            };
        },
    };
    const fakeHttp = {
        Agent: class {
            constructor() { agents.push(this); }
            destroy() { this.destroyed = true; }
        },
        request(...args) {
            if (constructionError) throw constructionError;
            const req = new EventEmitter();
            req.options = args[0] instanceof URL ? args[1] : args[0];
            req.written = [];
            req.write = (body) => {
                if (writeError) throw writeError;
                req.written.push(body);
            };
            req.end = () => {};
            req.destroy = () => { req.destroyed = true; };
            req.respond = () => {
                const response = new EventEmitter();
                response.statusCode = 200;
                response.headers = { 'content-type': 'application/octet-stream' };
                response.complete = false;
                response.destroy = () => { response.destroyed = true; };
                args[args.length - 1](response);
                return response;
            };
            requests.push(req);
            return req;
        },
    };
    const fakeProcess = {
        env: { HARBOR_ISOLATED: '1' },
        cwd: () => '/fake-repo',
        chdir: () => {},
        argv: [],
        stdout: {
            write(value, callback) {
                output.push(value);
                if (callback) callback();
            },
        },
        exit(code) { exitCodes.push(code); },
    };
    const sandbox = {
        Buffer, URL, console,
        module: { exports: {} },
        process: fakeProcess,
        require(name) {
            if (name === 'http') return fakeHttp;
            if (name.endsWith('/bin/app.js')) return app;
            if (name.endsWith('/bin/config.js')) return { auth: {} };
            return require(name);
        },
        setTimeout(callback, delay) {
            const timer = { callback, delay };
            timers.add(timer);
            return timer;
        },
        clearTimeout(timer) { timers.delete(timer); },
    };
    vm.createContext(sandbox);
    vm.runInContext(harness + '\nthis.workerSource = WORKER_SOURCE;', sandbox);
    sandbox.workerSource = sandbox.workerSource.replace(
        '__HARBOR_INPUT__', Buffer.from(JSON.stringify(workerSpec)).toString('base64')
    );
    const workerSandbox = { ...sandbox, module: { exports: {} } };
    vm.createContext(workerSandbox);
    vm.runInContext(sandbox.workerSource.split('\nmain().catch(')[0], workerSandbox);
    return {
        timers, requests, agents, output, exitCodes, sandbox, workerSandbox,
        get closeCount() { return closeCount; },
        set constructionError(error) { constructionError = error; },
        set writeError(error) { writeError = error; },
        fireDeadline() {
            assert.strictEqual(timers.size, 1, 'one absolute request deadline');
            const timer = [...timers][0];
            assert.strictEqual(timer.delay, 10000);
            timer.callback();
        },
    };
}

async function checkTransport(kind) {
    let checks = 0;
    for (const scenario of [
        'success', 'request-error', 'request-close', 'response-error',
        'response-aborted', 'response-close', 'incomplete-end', 'no-headers',
        'endless-body', 'construction-error', 'write-error',
    ]) {
        const env = environment();
        const originalError = Object.assign(new Error(scenario), { code: 'TEST_FAILURE' });
        if (scenario === 'construction-error') env.constructionError = originalError;
        if (scenario === 'write-error') env.writeError = originalError;
        const payload = Buffer.from([0, 255, 1, 2]);
        const promise = kind === 'worker'
            ? env.workerSandbox.requestHttp(1234, {
                method: 'POST', path: '/zip?path=unicode',
                headers: { 'content-length': payload.length }, bodyBase64: payload.toString('base64'),
            })
            : env.sandbox.request('http://127.0.0.1:1234', '/zip?path=unicode', {
                method: 'POST', headers: { 'content-length': payload.length }, body: payload,
            });
        const req = env.requests[0];
        let response;
        let expectedCode = 'TEST_FAILURE';
        if (scenario === 'request-error') req.emit('error', originalError);
        if (scenario === 'request-close') {
            req.emit('close');
            expectedCode = 'ERR_HTTP_REQUEST_CLOSED';
        }
        if (scenario === 'no-headers') {
            env.fireDeadline();
            expectedCode = 'ERR_HTTP_REQUEST_TIMEOUT';
        }
        if (['success', 'response-error', 'response-aborted', 'response-close', 'incomplete-end', 'endless-body'].includes(scenario)) {
            response = req.respond();
            response.emit('data', Buffer.from([0, 255]));
            if (scenario === 'success') {
                response.emit('data', Buffer.from([1, 2]));
                response.complete = true;
                response.emit('end');
                response.emit('close');
            } else if (scenario === 'response-error') response.emit('error', originalError);
            else if (scenario === 'response-aborted') {
                response.emit('aborted');
                expectedCode = 'ERR_HTTP_RESPONSE_ABORTED';
            } else if (scenario === 'response-close') {
                response.emit('close');
                expectedCode = 'ERR_HTTP_RESPONSE_INCOMPLETE';
            } else if (scenario === 'incomplete-end') {
                response.emit('end');
                expectedCode = 'ERR_HTTP_RESPONSE_INCOMPLETE';
            } else {
                // Receiving more bytes must not reset the absolute deadline.
                response.emit('data', Buffer.from('still not finished'));
                env.fireDeadline();
                expectedCode = 'ERR_HTTP_REQUEST_TIMEOUT';
            }
        }
        if (scenario === 'success') {
            const value = await promise;
            assert.strictEqual(value.status, 200);
            assert.deepStrictEqual(kind === 'worker' ? Buffer.from(value.bodyBase64, 'base64') : value.body, payload);
            assert.ok(!req.destroyed, 'successful keep-alive request stays reusable');
        } else {
            await assert.rejects(promise, (error) => error.code === expectedCode);
            if (req) assert.strictEqual(req.destroyed, true, 'failed request destroyed');
            if (response) assert.strictEqual(response.destroyed, true, 'failed response destroyed');
        }
        assert.strictEqual(env.timers.size, 0, 'deadline cleared on every terminal path');
        if (req) {
            assert.strictEqual(req.options.headers['Content-Length'], undefined, 'no duplicate content length');
            // Destruction can emit late errors. They must remain handled and
            // must not settle the request twice or turn failure into success.
            req.emit('error', new Error('late request error'));
        }
        if (response) {
            response.emit('error', new Error('late response error'));
            response.emit('end');
            response.emit('close');
        }
        checks += 1;
    }
    console.log('PASS ' + kind + ' HTTP transport: ' + checks + ' fault/success scenarios');
}

async function checkWorkerFailureExit(timeout) {
    const env = environment({
        repo: '/fake-repo', kind: 'start-http', options: {}, request: { path: '/zip' },
    });
    // Run the unchanged production entry point, including its failure catch.
    vm.runInContext(env.sandbox.workerSource.slice(env.sandbox.workerSource.indexOf('\nmain().catch(')), env.workerSandbox);
    await Promise.resolve();
    await Promise.resolve();
    assert.strictEqual(env.requests.length, 1);
    if (timeout) env.fireDeadline();
    else env.requests[0].emit('error', new Error('connection reset'));
    for (let i = 0; i < 20; i += 1) await Promise.resolve();
    assert.strictEqual(env.closeCount, 1, 'candidate close runs on request failure');
    assert.ok(env.agents.every((agent) => agent.destroyed), 'worker agent destroyed');
    const result = JSON.parse(env.output.join('').trim().split('=').slice(1).join('='));
    assert.strictEqual(result.ok, false);
    assert.ok(result.error.includes(timeout
        ? 'GET /zip: HTTP request timed out after 10000ms' : 'connection reset'));
    assert.deepStrictEqual(env.exitCodes, [1], 'failure flushed before worker exits');
    assert.strictEqual(env.timers.size, 0);
    console.log('PASS worker ' + (timeout ? 'deadline' : 'error') + ': cleanup, authenticated diagnostic, and exit');
}

(async () => {
    await checkTransport('controller');
    await checkTransport('worker');
    await checkWorkerFailureExit(false);
    await checkWorkerFailureExit(true);
})().catch((error) => {
    console.error(error.stack || error);
    process.exitCode = 1;
});
