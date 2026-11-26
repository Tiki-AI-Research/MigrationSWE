// Shared entry used by both harness commands. `side` selects which side of the
// observation inventory is dispatched; the remaining argv is the report path.
import { main } from './run.mjs';

const side = process.argv[2];
const argv = process.argv.slice(3);

try {
  await main({ side, argv });
} catch (error) {
  process.stderr.write(`${error && error.stack ? error.stack : String(error)}\n`);
  process.exitCode = 1;
}
