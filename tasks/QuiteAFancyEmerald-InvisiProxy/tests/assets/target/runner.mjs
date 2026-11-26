#!/usr/bin/env node
// Target-side integration entry point for the Fastify -> Express migration.
//
// The target runtime image is the candidate Express 5.1.0 implementation.
// Running this entry point executes every required observation against that
// repository and writes a `migration-observations:v6` report.
import { run, writeReport } from '../shared/integration/harness.mjs';

const reportPath = process.argv[2] || '/logs/result.json';
const report = await run({ side: 'target' });
writeReport(report, reportPath);
