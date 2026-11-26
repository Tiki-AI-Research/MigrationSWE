'use strict';

// Sent on stdin to Node inside the candidate sandbox. The controller owns the
// verdict; this probe observes the candidate smoke's use of the real export.
const Module = require('module');
const path = require('path');
const state = { builds: 0, injections: 0, closes: 0, responseReads: 0, assertionFailures: 0 };
const instances = new Set();
const closed = new Set();
const proxies = new WeakMap();

function observedAssert(target) {
    if (typeof target !== 'function') return target;
    if (proxies.has(target)) return proxies.get(target);
    const proxy = new Proxy(target, {
        apply(fn, receiver, args) {
            try {
                return Reflect.apply(fn, receiver, args);
            } catch (error) {
                if (state.responseReads > 0 && error.code === 'ERR_ASSERTION') state.assertionFailures++;
                throw error;
            }
        },
        get(fn, key) { return observedAssert(Reflect.get(fn, key)); },
    });
    proxies.set(target, proxy);
    return proxy;
}

const load = Module._load;
Module._load = function (request, parent, isMain) {
    const loaded = Reflect.apply(load, this, arguments);
    return /^(?:node:)?assert(?:\/strict)?$/.test(request) ? observedAssert(loaded) : loaded;
};

// This exact module path is the task's application contract. The smoke must
// call this export, even when it imports it through an alias or helper.
const appModule = require(path.resolve('bin/app.js'));
const buildApp = appModule.buildApp;
appModule.buildApp = function () {
    const app = Reflect.apply(buildApp, this, arguments);
    state.builds++;
    if (instances.has(app)) return app;
    instances.add(app);
    const inject = app.inject;
    const close = app.close;

    function response(value) {
        state.injections++;
        return new Proxy(value, {
            get(target, key) {
                if (['statusCode', 'body', 'payload', 'rawPayload', 'headers', 'json'].includes(key)) {
                    state.responseReads++;
                    if (mutate) {
                        if (key === 'statusCode') return 599;
                        if (key === 'headers') return {};
                        if (key === 'rawPayload') return Buffer.from('corrupted smoke response');
                        if (key === 'json') return () => ({ corrupted: true });
                        return 'corrupted smoke response';
                    }
                }
                return Reflect.get(target, key);
            },
        });
    }

    app.inject = function (...args) {
        const callback = args[args.length - 1];
        if (typeof callback === 'function') {
            args[args.length - 1] = (error, value) => callback(error, error ? value : response(value));
            return Reflect.apply(inject, this, args);
        }
        return Promise.resolve(Reflect.apply(inject, this, args)).then(response);
    };
    app.close = function (...args) {
        function completed() {
            closed.add(app);
            state.closes = closed.size;
        }
        const callback = args[args.length - 1];
        if (typeof callback === 'function') {
            args[args.length - 1] = error => { if (!error) completed(); callback(error); };
            return Reflect.apply(close, this, args);
        }
        return Promise.resolve(Reflect.apply(close, this, args)).then(value => { completed(); return value; });
    };
    return app;
};

process.on('exit', () => {
    process.stdout.write('\nHARBOR_SMOKE_EXECUTION=' + JSON.stringify({ ...state, instances: instances.size }) + '\n');
});
require(path.resolve('test/fastify-smoke.js'));
