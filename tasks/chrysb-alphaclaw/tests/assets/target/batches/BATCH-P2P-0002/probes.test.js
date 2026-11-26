const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const loadServerLifecycle = () =>
  require(path.join(process.cwd(), "lib", "server", "init", "server-lifecycle.js"));

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("shutdown signals stop gmail watch and dispose terminal", async () => {
  const { registerServerShutdown } = loadServerLifecycle();
  const listeners = new Map();
  const originalOn = process.on;
  const calls = [];

  process.on = (event, handler) => {
    listeners.set(event, handler);
    return process;
  };

  try {
    const gmailWatchService = {
      stop: async () => {
        calls.push("gmail");
      },
    };
    const watchdogTerminal = {
      disposeSession: () => {
        calls.push("terminal");
      },
    };

    registerServerShutdown({ gmailWatchService, watchdogTerminal });

    assert.equal(typeof listeners.get("SIGTERM"), "function");
    assert.equal(typeof listeners.get("SIGINT"), "function");

    listeners.get("SIGTERM")();
    await flush();
    assert.deepEqual(calls, ["gmail", "terminal"]);

    listeners.get("SIGINT")();
    await flush();
    assert.equal(calls.filter((entry) => entry === "gmail").length, 2);
    assert.equal(calls.filter((entry) => entry === "terminal").length, 2);
  } finally {
    process.on = originalOn;
  }
});
