import { writeFileSync } from 'node:fs';

import { run } from '../../../shared/batches/BATCH-DIFFERENTIAL-0001/runner.mjs';

const root = process.env.OIDC_REPO_ROOT ?? '/input/target';

const result = await run(root);
const serialized = `${JSON.stringify(result, null, 2)}\n`;

if (process.argv[2]) {
  writeFileSync(process.argv[2], serialized);
} else {
  process.stdout.write(serialized);
}
