#!/usr/bin/env node

const {
  runObservation,
} = require("../../../shared/batches/BATCH-F2P-0003/probe-lib.js");

runObservation({
  observationId: "F2P-065",
  side: "target",
  scenario: "inProcess",
  requirementIds: ["R039"],
});
