// Executes the BATCH-P2P-0002 behavioural checks against whatever package the
// workspace root exposes. Run from this directory's workspace with `node`.

import * as devalue from '../../../index.js';
import { createObservations } from '../../../shared/batches/BATCH-P2P-0002/observations.js';

const checks = createObservations(devalue);
const results = [];

for (const [observation_id, run] of Object.entries(checks)) {
	try {
		await run();
		results.push({ observation_id, status: 'PASS' });
	} catch (error) {
		results.push({
			observation_id,
			status: 'FAIL',
			error: error && error.stack ? error.stack : String(error)
		});
	}
}

process.stdout.write(`${JSON.stringify({ batch_id: 'BATCH-P2P-0002', results }, null, 2)}\n`);

if (results.some((result) => result.status !== 'PASS')) {
	process.exitCode = 1;
}
