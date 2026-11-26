// Target-side entry for the BATCH-F2P-0002 F2P observations.
//
// Run with the shared loader so the executed module graph can be observed:
//   BATCH_F2P_0002_TRACE_OUT=<file> node --experimental-loader \
//     ../../../shared/batches/BATCH-F2P-0002/hooks.mjs ./probe.mjs <target-root>
//
// Every F2P observation must report PASS on the Fastify target tree; the shared
// predicate in ../../../shared/batches/BATCH-F2P-0002/probe.mjs is evaluated
// identically on both sides.
import { pathToFileURL } from 'node:url';
import { runAll } from '../../../shared/batches/BATCH-F2P-0002/probe.mjs';

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  const report = await runAll();
  process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
  process.exit(0);
}

export { runAll };
