#!/usr/bin/env node
"use strict";

const { run } = require("./shared/integration/run-observations.js");

run({ side: "target" }).catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exitCode = 1;
});
