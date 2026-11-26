import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { run_side } from '../../shared/integration/run-side.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const root = join(here, '..', '..');

const results = await run_side(root, 'target');

process.stdout.write(`${JSON.stringify(results, null, 2)}\n`);

if (!results.ok) {
	process.exitCode = 1;
}
