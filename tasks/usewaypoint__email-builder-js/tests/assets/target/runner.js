#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');

const BATCH_ID = 'BATCH-INTEGRATION-0001';
const SCHEMA_VERSION = 'migration-observations:v6';
const observationsPath = path.join(__dirname, '..', 'shared', 'integration', 'observations.json');

function loadInventory() {
  try {
    return JSON.parse(fs.readFileSync(observationsPath, 'utf8'));
  } catch (error) {
    return [];
  }
}

function normalizeReport(side, candidate, error) {
  const inventory = loadInventory();
  const observed = new Map();
  if (candidate && Array.isArray(candidate.observations)) {
    for (const observation of candidate.observations) {
      if (observation && typeof observation.id === 'string') {
        observed.set(observation.id, observation);
      }
    }
  }
  const message = error ? String(error && error.message ? error.message : error) : 'observation was not produced';
  const observations = inventory.map((item) => {
    const observation = observed.get(item.id);
    if (observation && observation.kind === item.kind && observation.status && observation.value) {
      return {
        id: item.id,
        kind: item.kind,
        status: observation.status,
        value: observation.value,
      };
    }
    return {
      id: item.id,
      kind: item.kind,
      status: 'INCONCLUSIVE',
      value: { type: 'json', value: { error: message } },
    };
  });
  if (observations.length === 0) {
    for (const observation of observed.values()) {
      if (observation && observation.id && observation.kind && observation.status && observation.value) {
        observations.push({
          id: observation.id,
          kind: observation.kind,
          status: observation.status,
          value: observation.value,
        });
      }
    }
  }
  return {
    schema_version: SCHEMA_VERSION,
    batch_id: BATCH_ID,
    side,
    observations,
  };
}

function writeReport(report, reportPath) {
  const rendered = JSON.stringify(report, null, 2) + '\n';
  if (reportPath) {
    const resolved = path.resolve(reportPath);
    fs.mkdirSync(path.dirname(resolved), { recursive: true });
    fs.writeFileSync(resolved, rendered);
  }
  process.stdout.write(rendered);
}

let report;
try {
  const integration = require(path.join(__dirname, '..', 'shared', 'integration', 'index.js'));
  report = normalizeReport('target', integration.run('target'), null);
} catch (error) {
  report = normalizeReport('target', null, error);
}

try {
  writeReport(report, process.argv[2]);
} catch (error) {
  process.stderr.write(String(error && error.stack ? error.stack : error) + '\n');
  process.exitCode = 1;
}
