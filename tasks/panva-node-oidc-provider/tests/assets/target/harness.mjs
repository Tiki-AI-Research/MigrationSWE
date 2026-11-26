// Target-side integration entry point.
//
// Invoked as: node {assets}/target/harness.mjs {report}
// The repository under observation is selected through MIGRATION_REPO_ROOT /
// MIGRATION_REPO / REPO_ROOT / REPO (default /workspace/repo).
import { main } from '../shared/integration/run.mjs';

try {
  await main({ side: 'target', argv: process.argv.slice(2) });
} catch (error) {
  process.stderr.write(`${error && error.stack ? error.stack : String(error)}\n`);
  process.exitCode = 1;
}
