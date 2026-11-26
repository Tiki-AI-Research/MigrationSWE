#!/usr/bin/env node

const {
  runObservation,
} = require("../../../shared/batches/BATCH-F2P-0003/probe-lib.js");

runObservation({
  observationId: "F2P-069",
  side: "target",
  scenario: "healthHono",
  requirementIds: ["R001"],
});
